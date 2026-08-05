"""实验 API — 回测实验的完整生命周期管理."""

from __future__ import annotations

import hashlib
import itertools
import json as _json
import math
import sqlite3
from collections.abc import Mapping
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.dependencies import (
    get_db,
    get_job_broker,
    require_permission,
)
from backend.strategies.base import StrategyCategory
from backend.services.research_risk import research_risk_summary
from backend.services.experiment_eligibility import (
    assess_experiment_eligibility,
    load_experiment_eligibility,
)
from backend.services.research_evidence_export import (
    JSON_MEDIA_TYPE,
    ZIP_MEDIA_TYPE,
    ResearchEvidenceExportError,
    build_csv_zip_evidence,
    prepare_research_evidence,
    stream_binary_file,
    stream_json_evidence,
)
from backend.api.schemas import (
    ApiResponse,
    EquityPointResponse,
    ExperimentPage,
    ExperimentResponse,
    ExperimentSortKey,
    ExperimentSortOrder,
    IdJobResponse,
    MetricsResponse,
    Page,
    ParameterPresetResponse,
    TradeResponse,
)
from backend.api.storage_paths import redact_model_storage_paths
from backend.api.timestamps import (
    serialize_utc_timestamp,
    serialize_utc_timestamp_fields,
)

router = APIRouter(prefix="/api/experiments", tags=["Experiments"])

MAX_SWEEP_EXPERIMENTS = 100
MAX_SWEEP_PARAMETERS = 10
MAX_SWEEP_VALUES_PER_PARAMETER = 100
DataAccessPolicy = Literal["allow_fetch", "cache_only"]
ResearchTrustProfile = Literal[
    "governed_production_pit",
    "tushare_research_trusted",
]
EXPERIMENT_SORT_COLUMNS: dict[ExperimentSortKey, str] = {
    "created_at": "e.created_at",
    "annual_return": "m.annual_return",
    "sharpe_ratio": "m.sharpe_ratio",
    "max_drawdown": "m.max_drawdown",
    "strategy_id": "e.strategy_id",
    "status": "e.status",
}
EXPERIMENT_METRIC_SORT_KEYS = frozenset(
    {"annual_return", "sharpe_ratio", "max_drawdown"}
)


def _is_repairable_sweep_error(value: Any) -> bool:
    message = str(value or "").lower()
    return (
        "database is locked" in message
        or "database table is locked" in message
        or "sqlite_busy" in message
        or "sqlite_locked" in message
    )


def _is_manifest_retry_conflict(value: Any) -> bool:
    message = str(value or "")
    return "ManifestConflictError" in message


def _is_repairable_sweep_member(
    error_log: Any,
    *,
    prior_transient_job_failure: bool,
) -> bool:
    return _is_repairable_sweep_error(error_log) or (
        prior_transient_job_failure
        and _is_manifest_retry_conflict(error_log)
    )


async def _sweep_repair_evidence_sql(conn: Any) -> tuple[str, str]:
    cursor = await conn.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type='table' AND name IN ('jobs', 'research_run_manifests')
        """
    )
    available = {str(row["name"]) for row in await cursor.fetchall()}
    prior_job_sql = (
        """
        EXISTS(
            SELECT 1 FROM jobs j
            WHERE j.resource_type='experiment'
              AND CAST(j.resource_id AS INTEGER)=e.id
              AND (
                  lower(COALESCE(j.error, '')) LIKE '%database is locked%'
                  OR lower(COALESCE(j.error, '')) LIKE '%database table is locked%'
                  OR lower(COALESCE(j.error, '')) LIKE '%sqlite_busy%'
                  OR lower(COALESCE(j.error, '')) LIKE '%sqlite_locked%'
              )
        )
        """
        if "jobs" in available
        else "0"
    )
    manifest_sql = (
        """
        EXISTS(
            SELECT 1 FROM research_run_manifests rm
            WHERE rm.experiment_id=e.id
        )
        """
        if "research_run_manifests" in available
        else "0"
    )
    return prior_job_sql, manifest_sql


# ── Pydantic 请求体 ──────────────────────────────────────────────────────────

class CreateExperimentBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    strategy_id: str
    pool_preset: str | None = None
    pool_custom_codes: str | list[str] | None = None
    pool_industries: str | list[str] | None = None
    train_start: str | None = None
    train_end: str | None = None
    test_start: str
    test_end: str
    params: dict[str, Any] = Field(default_factory=dict)
    mode: Literal["batch", "realtime"] = "batch"
    data_access_policy: DataAccessPolicy = "cache_only"
    research_trust_profile: ResearchTrustProfile = "governed_production_pit"
    source_experiment_id: int | None = Field(None, ge=1)

    @model_validator(mode="after")
    def validate_windows(self):
        from datetime import date

        test_start = date.fromisoformat(self.test_start)
        test_end = date.fromisoformat(self.test_end)
        if test_start >= test_end:
            raise ValueError("test_start 必须早于 test_end")
        if bool(self.train_start) != bool(self.train_end):
            raise ValueError("train_start 和 train_end 必须同时提供")
        if self.train_start and self.train_end:
            train_start = date.fromisoformat(self.train_start)
            train_end = date.fromisoformat(self.train_end)
            if train_start >= train_end:
                raise ValueError("train_start 必须早于 train_end")
            if train_end >= test_start:
                raise ValueError("训练窗口必须在测试窗口之前结束")
        if self.pool_preset == "custom" and not self.pool_custom_codes:
            raise ValueError("自定义股票池必须提供股票代码")
        return self


class SweepBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_id: str
    name: str | None = None
    param_grid: dict[str, list[Any]]  # {param_name: [v1, v2, v3], ...}
    pool_preset: str | None = None
    pool_custom_codes: str | None = None
    pool_industries: str | None = None
    train_start: str | None = None
    train_end: str | None = None
    # ``test_*`` is retained only for backward compatibility. New clients must
    # provide an explicit selection/validation window plus a locked test window.
    test_start: str | None = None
    test_end: str | None = None
    selection_start: str | None = None
    selection_end: str | None = None
    validation_start: str | None = None
    validation_end: str | None = None
    locked_test_start: str | None = None
    locked_test_end: str | None = None
    base_params: dict[str, Any] = Field(default_factory=dict)
    mode: Literal["batch", "realtime"] = "batch"
    data_access_policy: DataAccessPolicy = "cache_only"
    research_trust_profile: ResearchTrustProfile = "governed_production_pit"
    source_experiment_id: int | None = Field(None, ge=1)

    @model_validator(mode="after")
    def validate_windows(self):
        from datetime import date

        legacy_pair = bool(self.test_start), bool(self.test_end)
        selection_pair = bool(self.selection_start), bool(self.selection_end)
        validation_pair = bool(self.validation_start), bool(self.validation_end)
        locked_pair = bool(self.locked_test_start), bool(self.locked_test_end)
        for pair, label in (
            (legacy_pair, "test_start 和 test_end"),
            (selection_pair, "selection_start 和 selection_end"),
            (validation_pair, "validation_start 和 validation_end"),
            (locked_pair, "locked_test_start 和 locked_test_end"),
        ):
            if pair[0] != pair[1]:
                raise ValueError(f"{label} 必须同时提供")
        if all(selection_pair) and all(validation_pair):
            raise ValueError("selection 与 validation 窗口只能提供一组")

        explicit_pair = selection_pair if all(selection_pair) else validation_pair
        if all(explicit_pair):
            if any(legacy_pair):
                raise ValueError("显式选模窗口不能与旧版 test 窗口混用")
            if not all(locked_pair):
                raise ValueError("显式选模窗口必须同时提供完整 locked_test 窗口")
            selection_start = date.fromisoformat(
                self.selection_start or self.validation_start or ""
            )
            selection_end = date.fromisoformat(
                self.selection_end or self.validation_end or ""
            )
            locked_start = date.fromisoformat(self.locked_test_start or "")
            locked_end = date.fromisoformat(self.locked_test_end or "")
            if selection_start >= selection_end:
                raise ValueError("选模窗口起始日期必须早于结束日期")
            if locked_start >= locked_end:
                raise ValueError("locked_test_start 必须早于 locked_test_end")
            if selection_end >= locked_start:
                raise ValueError(
                    "选模窗口与锁定测试窗口必须严格分离: "
                    "validation_end < locked_test_start"
                )
        else:
            if not all(legacy_pair):
                raise ValueError(
                    "必须提供显式选模窗口，或兼容用的完整 test 窗口"
                )
            if any(locked_pair):
                raise ValueError("locked_test 窗口不能与旧版 test 窗口混用")
            selection_start = date.fromisoformat(self.test_start or "")
            selection_end = date.fromisoformat(self.test_end or "")
            if selection_start >= selection_end:
                raise ValueError("test_start 必须早于 test_end")

        if bool(self.train_start) != bool(self.train_end):
            raise ValueError("train_start 和 train_end 必须同时提供")
        if self.train_start and self.train_end:
            train_start = date.fromisoformat(self.train_start)
            train_end = date.fromisoformat(self.train_end)
            if train_start >= train_end:
                raise ValueError("train_start 必须早于 train_end")
            if train_end >= selection_start:
                raise ValueError("训练窗口必须在选模窗口之前结束")
        if self.pool_preset == "custom" and not self.pool_custom_codes:
            raise ValueError("自定义股票池必须提供股票代码")
        return self

    def selection_window(self) -> tuple[str, str]:
        """Return the canonical window used by sweep member experiments."""
        return (
            self.selection_start or self.validation_start or self.test_start or "",
            self.selection_end or self.validation_end or self.test_end or "",
        )

    def research_trust(self) -> str:
        return (
            "locked_test"
            if self.locked_test_start and self.locked_test_end
            else "legacy_unlocked"
        )


class PromoteSweepBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    experiment_id: int = Field(ge=1)


class CompareBody(BaseModel):
    experiment_ids: list[int]


class StarBody(BaseModel):
    is_starred: bool


class LabelsBody(BaseModel):
    labels: list[str]


class CreateParameterPresetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    strategy_id: str = Field(min_length=1, max_length=100)
    params: dict[str, Any] = Field(default_factory=dict)
    mode: Literal["batch", "realtime"] = "batch"
    pool_preset: str | None = None
    pool_custom_codes: str | list[str] | None = None
    pool_industries: str | list[str] | None = None
    source_experiment_id: int | None = Field(None, ge=1)
    metrics_snapshot: dict[str, Any] | None = None
    notes: str | None = Field(None, max_length=2000)
    labels: list[str] = Field(default_factory=list)
    is_default: bool = False


class UpdateParameterPresetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(None, min_length=1, max_length=100)
    params: dict[str, Any] | None = None
    mode: Literal["batch", "realtime"] | None = None
    pool_preset: str | None = None
    pool_custom_codes: str | list[str] | None = None
    pool_industries: str | list[str] | None = None
    source_experiment_id: int | None = Field(None, ge=1)
    metrics_snapshot: dict[str, Any] | None = None
    notes: str | None = Field(None, max_length=2000)
    labels: list[str] | None = None
    is_default: bool | None = None


# ═══════════════════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════════════════

def _hash_params(params: dict[str, Any]) -> str:
    """计算参数 hash（用于去重）。"""
    raw = _json.dumps(params, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(raw.encode()).hexdigest()


def _training_mode(strategy_meta: Any) -> str:
    """Return the platform training contract derived from strategy metadata."""
    if not strategy_meta.requires_training:
        return "none"
    frequency = (
        strategy_meta.retrain_frequency.value
        if hasattr(strategy_meta.retrain_frequency, "value")
        else str(strategy_meta.retrain_frequency)
    )
    return "train_once" if frequency == "never" else "periodic"


def _require_training_window(strategy_meta: Any) -> bool:
    """Only train-once models require a user-selected immutable train window."""
    return _training_mode(strategy_meta) == "train_once"


async def _require_pit_submission(
    *,
    pool_id: str | None,
    train_start: str | None,
    test_start: str,
    test_end: str,
    data_access_policy: DataAccessPolicy,
    research_trust_profile: ResearchTrustProfile = "governed_production_pit",
    purpose: Literal["research", "tuning"],
    research_generation_id: str | None = None,
) -> dict[str, Any] | None:
    """Reject before persistence when immutable PIT evidence is unavailable."""

    import pandas as pd

    from backend.data.pit_runtime import (
        PitRuntimeDataError,
        require_pit_pool,
        require_pit_runtime_input,
    )

    if data_access_policy != "cache_only":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "pit_cache_only_required",
                "message": (
                    "PIT-only 模式禁止实验运行时联网补数；"
                    "数据更新只能进入治理隔离区"
                ),
            },
        )
    calculation_start = (
        pd.Timestamp(train_start or test_start) - pd.Timedelta(days=400)
    ).strftime("%Y-%m-%d")
    if research_trust_profile == "tushare_research_trusted":
        normalized_pool = require_pit_pool(pool_id)
        from backend.data.cache import resolve_pool_benchmark
        from backend.services.research_runtime import (
            ResearchRuntimeError,
            build_research_trust,
            load_research_benchmark,
            load_research_market,
        )

        try:
            market = await load_research_market(
                pool_id=normalized_pool,
                required_start=calculation_start,
                required_end=test_end,
                generation_id=research_generation_id,
            )
        except ResearchRuntimeError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": exc.code,
                    "message": exc.message,
                    "technical_blockers": list(
                        exc.report.get("issues") or [exc.code]
                    ),
                    "retryable_after_research_refresh": True,
                },
            ) from exc
        benchmark_start = (
            pd.Timestamp(test_start) - pd.Timedelta(days=10)
        ).strftime("%Y-%m-%d")
        generation_id = str(market["report"]["generation_id"])
        try:
            benchmark = await load_research_benchmark(
                index_code=resolve_pool_benchmark(normalized_pool),
                required_start=benchmark_start,
                required_end=test_end,
                generation_id=generation_id,
            )
        except ResearchRuntimeError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": exc.code,
                    "message": exc.message,
                    "technical_blockers": list(
                        exc.report.get("issues") or [exc.code]
                    ),
                    "retryable_after_research_refresh": True,
                },
            ) from exc
        return build_research_trust(
            market_result=market,
            required_start=calculation_start,
            required_end=test_end,
            purpose="real_tuning" if purpose == "tuning" else "return_research",
            benchmark_report=benchmark.get("report") or {},
        )
    try:
        await require_pit_runtime_input(
            pool_id=pool_id,
            required_start=calculation_start,
            required_end=test_end,
            purpose=purpose,
        )
    except PitRuntimeDataError as exc:
        # 测试分支放宽（v0.8.x 分级门禁）：研究/模拟用途在 PIT 数据未激活时
        # 降级为"告警放行"，允许使用可用缓存数据运行实验；实盘（L3，当前关闭）
        # 保持硬锁语义不受影响。风险由研究清单与运行时数据校验兜底。
        return {
            "trust": "research_degraded_no_pit",
            "warnings": [
                f"PIT 数据未激活，实验将使用缓存数据运行（{exc.code}）；"
                "结果仅供研究参考，不可作为实盘依据"
            ],
        }
    return None


def _row_to_dict(row: Any) -> dict[str, Any]:
    """将 aiosqlite.Row 转为普通 dict。"""
    if row is None:
        return {}
    return dict(serialize_utc_timestamp_fields(dict(row)))


def _data_access_policy_from_run_spec(value: Any) -> DataAccessPolicy:
    """Read the durable policy; legacy experiments retain fetch behaviour."""

    if not value:
        return "allow_fetch"
    try:
        run_spec = _json.loads(value) if isinstance(value, str) else value
    except (_json.JSONDecodeError, TypeError):
        return "allow_fetch"
    if not isinstance(run_spec, dict):
        return "allow_fetch"
    policy = run_spec.get("data_access_policy", "allow_fetch")
    if policy not in {"allow_fetch", "cache_only"}:
        raise HTTPException(
            status_code=409,
            detail="实验持久化的数据访问策略无效",
        )
    return "cache_only" if policy == "cache_only" else "allow_fetch"


def _research_trust_from_run_spec(value: Any) -> dict[str, Any]:
    try:
        run_spec = _json.loads(value) if isinstance(value, str) else value
    except (_json.JSONDecodeError, TypeError):
        run_spec = {}
    if not isinstance(run_spec, dict):
        run_spec = {}
    trust = run_spec.get("research_trust")
    if (
        run_spec.get("research_trust_profile") == "tushare_research_trusted"
        and isinstance(trust, dict)
    ):
        return {
            "profile": "tushare_research_trusted",
            "trust_tier": "conditional_personal_research",
            "known_limitations": list(trust.get("known_limitations") or []),
            "warning_severity": str(trust.get("warning_severity") or "high"),
            "eligible_for_paper_trading": True,
            "eligible_for_live_trading": False,
        }
    return {
        "profile": "governed_production_pit",
        "trust_tier": "governed_production_pit",
        "known_limitations": [],
        "eligible_for_paper_trading": False,
        "eligible_for_live_trading": False,
    }


async def _research_risk_summaries(
    conn: Any,
    experiment_ids: list[int],
    user: dict[str, Any],
) -> dict[int, dict[str, Any]]:
    """Load only manifest evidence for experiments visible to ``user``."""
    if not experiment_ids:
        return {}
    placeholders = ",".join("?" for _ in experiment_ids)
    conditions = [f"r.experiment_id IN ({placeholders})"]
    params: list[Any] = [*experiment_ids]
    if not user.get("is_admin"):
        conditions.append("e.user_id = ?")
        params.append(user["id"])
    cursor = await conn.execute(
        f"""
        SELECT r.experiment_id, r.schema_version, r.manifest_json,
               r.manifest_hash, e.strategy_id
        FROM research_run_manifests r
        JOIN experiments e ON e.id = r.experiment_id
        WHERE {" AND ".join(conditions)}
        """,
        params,
    )
    summaries: dict[int, dict[str, Any]] = {}
    for row in await cursor.fetchall():
        experiment_id = int(row["experiment_id"])
        summary = research_risk_summary(
            manifest_json=row["manifest_json"],
            manifest_hash=row["manifest_hash"],
            schema_version=row["schema_version"],
        )
        eligibility = assess_experiment_eligibility(
            experiment_id=experiment_id,
            strategy_id=str(row["strategy_id"]),
            manifest_json=row["manifest_json"],
            manifest_hash=row["manifest_hash"],
            schema_version=row["schema_version"],
        )
        summary.update(eligibility.public_dict())
        summaries[experiment_id] = summary
    return summaries


def _serialize_list_field(value: str | list[str] | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        return _json.dumps(value, ensure_ascii=False)
    return value


def _deserialize_list_field(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    try:
        parsed = _json.loads(value)
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
    except (_json.JSONDecodeError, TypeError):
        pass
    return [
        item.strip()
        for item in value.replace("，", ",").split(",")
        if item.strip()
    ]


def _parameter_preset_to_dict(row: Any) -> dict[str, Any]:
    result = _row_to_dict(row)
    result["params"] = _json.loads(result.get("params") or "{}")
    result["metrics_snapshot"] = _json.loads(
        result.get("metrics_snapshot") or "{}"
    )
    result["labels"] = _json.loads(result.get("labels") or "[]")
    result["pool_custom_codes"] = _deserialize_list_field(
        result.get("pool_custom_codes")
    )
    result["pool_industries"] = _deserialize_list_field(
        result.get("pool_industries")
    )
    result["is_default"] = bool(result.get("is_default"))
    return result


async def _get_source_experiment(
    conn: Any,
    source_experiment_id: int,
    user: dict[str, Any],
    *,
    strategy_id: str | None = None,
    require_completed: bool = False,
) -> Any:
    query = "SELECT * FROM experiments WHERE id = ?"
    params: list[Any] = [source_experiment_id]
    if not user.get("is_admin"):
        query += " AND user_id = ?"
        params.append(user["id"])
    cursor = await conn.execute(query, params)
    row = await cursor.fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"来源实验不存在或无权访问: {source_experiment_id}",
        )
    if strategy_id is not None and row["strategy_id"] != strategy_id:
        raise HTTPException(status_code=422, detail="来源实验与参数方案的策略不一致")
    if require_completed and row["status"] != "completed":
        raise HTTPException(status_code=422, detail="只有已完成实验才能保存为参数方案")
    eligibility = await load_experiment_eligibility(
        conn,
        experiment_id=int(row["id"]),
        strategy_id=str(row["strategy_id"]),
    )
    if not eligibility.eligible:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "legacy_experiment_reuse_forbidden",
                "message": "该实验仅保留为历史审计记录，不能用于重跑、调优或参数候选",
                "eligibility_code": eligibility.code,
            },
        )
    return row


async def _snapshot_experiment_metrics(conn: Any, experiment_id: int) -> dict[str, Any]:
    cursor = await conn.execute(
        "SELECT * FROM experiment_metrics WHERE experiment_id = ?",
        (experiment_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return {}
    snapshot = dict(row)
    snapshot.pop("id", None)
    snapshot.pop("experiment_id", None)
    return snapshot


# ═══════════════════════════════════════════════════════════════════════════
# GET / — 实验列表
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/", response_model=ApiResponse[ExperimentPage])
async def list_experiments(
    strategy_id: str | None = Query(None),
    strategy_category: StrategyCategory | None = Query(None),
    status: str | None = Query(None),
    starred: bool | None = Query(None),
    is_starred: bool | None = Query(None),
    label: str | None = Query(None),
    search: str | None = Query(None, max_length=100),
    sort_by: ExperimentSortKey = Query("created_at"),
    sort_order: ExperimentSortOrder = Query("desc"),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    user: dict[str, Any] = Depends(require_permission("experiments:read")),
) -> dict[str, Any]:
    """获取实验列表，支持多条件筛选和分页。"""
    conditions: list[str] = []
    params: list[Any] = []

    if strategy_id:
        conditions.append("e.strategy_id = ?")
        params.append(strategy_id)

    if strategy_category is not None:
        conditions.append("e.strategy_category = ?")
        params.append(strategy_category.value)

    if status:
        conditions.append("e.status = ?")
        params.append(status)

    star_filter = is_starred if is_starred is not None else starred
    if star_filter is not None:
        conditions.append("e.is_starred = ?")
        params.append(1 if star_filter else 0)

    if label:
        conditions.append("e.labels LIKE ?")
        params.append(f"%{label}%")
    if search:
        conditions.append("(e.name LIKE ? OR e.strategy_id LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])

    # FIXED: reviewer issue #11 — user_id 过滤（非 admin 只能看自己的）
    if not user.get("is_admin"):
        conditions.append("e.user_id = ?")
        params.append(user["id"])

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    offset = (page - 1) * limit
    sort_column = EXPERIMENT_SORT_COLUMNS[sort_by]
    direction = "ASC" if sort_order == "asc" else "DESC"
    nulls_last = (
        f"{sort_column} IS NULL ASC, "
        if sort_by in EXPERIMENT_METRIC_SORT_KEYS
        else ""
    )
    order_by = f"{nulls_last}{sort_column} {direction}, e.id {direction}"

    try:
        async for conn in get_db("experiment"):
            # 总数
            cursor = await conn.execute(
                f"SELECT COUNT(*) as cnt FROM experiments e {where}",
                params,
            )
            total_row = await cursor.fetchone()
            total = total_row["cnt"] if total_row else 0

            # 数据
            cursor = await conn.execute(
                f"""
                SELECT
                    e.*,
                    m.sharpe_ratio, m.annual_return, m.max_drawdown, m.win_rate
                FROM experiments e
                LEFT JOIN experiment_metrics m ON e.id = m.experiment_id
                {where}
                ORDER BY {order_by}
                LIMIT ? OFFSET ?
                """,
                params + [limit, offset],
            )
            rows = await cursor.fetchall()
            risk_summaries = await _research_risk_summaries(
                conn,
                [int(row["id"]) for row in rows],
                user,
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询实验列表失败: {e}")

    items: list[dict[str, Any]] = []
    for row in rows:
        d = _row_to_dict(row)
        d["labels"] = _json.loads(d["labels"]) if d.get("labels") else []
        d["params"] = _json.loads(d["params"]) if d.get("params") else {}
        d["pool_custom_codes"] = _deserialize_list_field(d.get("pool_custom_codes"))
        d["pool_industries"] = _deserialize_list_field(d.get("pool_industries"))
        d["is_starred"] = bool(d.get("is_starred", 0))
        d["research_risk_summary"] = risk_summaries.get(
            int(d["id"]),
            research_risk_summary(
                manifest_json=None,
                manifest_hash=None,
                schema_version=None,
            ),
        )
        d["research_risk_summary"].setdefault("pit_eligible", False)
        d["research_risk_summary"].setdefault("legacy_read_only", True)
        d["research_risk_summary"].setdefault(
            "eligibility_code", "legacy_manifest_missing"
        )
        items.append(d)

    return {
        "data": {
            "items": items,
            "total": total,
            "page": page,
            "limit": limit,
            "sort_by": sort_by,
            "sort_order": sort_order,
        }
    }


# ═══════════════════════════════════════════════════════════════════════════
# 参数方案 — 必须在 /{experiment_id} 动态路由之前定义
# ═══════════════════════════════════════════════════════════════════════════

@router.get(
    "/parameter-presets",
    response_model=ApiResponse[Page[ParameterPresetResponse]],
)
async def list_parameter_presets(
    strategy_id: str | None = Query(None),
    search: str | None = Query(None, max_length=100),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    user: dict[str, Any] = Depends(require_permission("experiments:read")),
) -> dict[str, Any]:
    conditions = ["user_id = ?"]
    params: list[Any] = [user["id"]]
    if strategy_id:
        conditions.append("strategy_id = ?")
        params.append(strategy_id)
    if search:
        conditions.append("(name LIKE ? OR notes LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])
    where = " AND ".join(conditions)
    offset = (page - 1) * limit

    async for conn in get_db("experiment"):
        cursor = await conn.execute(
            f"SELECT COUNT(*) AS cnt FROM parameter_presets WHERE {where}",
            params,
        )
        total_row = await cursor.fetchone()
        cursor = await conn.execute(
            f"""
            SELECT * FROM parameter_presets
            WHERE {where}
            ORDER BY is_default DESC, updated_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            params + [limit, offset],
        )
        rows = await cursor.fetchall()
    return {
        "data": {
            "items": [_parameter_preset_to_dict(row) for row in rows],
            "total": total_row["cnt"] if total_row else 0,
            "page": page,
            "limit": limit,
        }
    }


@router.post(
    "/parameter-presets",
    response_model=ApiResponse[ParameterPresetResponse],
)
async def create_parameter_preset(
    body: CreateParameterPresetBody,
    user: dict[str, Any] = Depends(require_permission("experiments:create")),
) -> dict[str, Any]:
    from backend.dependencies import get_strategy_registry

    registry = get_strategy_registry()
    try:
        strategy_meta = registry.get_metadata(body.strategy_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"策略不存在: {body.strategy_id}")
    supported_modes = {
        item.value if hasattr(item, "value") else str(item)
        for item in strategy_meta.supported_modes
    }
    if body.mode not in supported_modes:
        raise HTTPException(status_code=422, detail=f"策略不支持运行模式 {body.mode}")
    is_valid, validation_error = registry.validate_params(
        body.strategy_id, body.params
    )
    if not is_valid:
        raise HTTPException(status_code=422, detail=f"策略参数无效: {validation_error}")

    try:
        async for conn in get_db("experiment"):
            metrics_snapshot = body.metrics_snapshot or {}
            if body.source_experiment_id is not None:
                await _get_source_experiment(
                    conn,
                    body.source_experiment_id,
                    user,
                    strategy_id=body.strategy_id,
                    require_completed=True,
                )
                if body.metrics_snapshot is None:
                    metrics_snapshot = await _snapshot_experiment_metrics(
                        conn, body.source_experiment_id
                    )
            if body.is_default:
                await conn.execute(
                    """
                    UPDATE parameter_presets SET is_default = 0,
                        updated_at = datetime('now')
                    WHERE user_id = ? AND strategy_id = ? AND is_default = 1
                    """,
                    (user["id"], body.strategy_id),
                )
            cursor = await conn.execute(
                """
                INSERT INTO parameter_presets
                    (user_id, name, strategy_id, params, mode, pool_preset,
                     pool_custom_codes, pool_industries, source_experiment_id,
                     metrics_snapshot, notes, labels, is_default)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user["id"],
                    body.name.strip(),
                    body.strategy_id,
                    _json.dumps(body.params, ensure_ascii=False),
                    body.mode,
                    body.pool_preset,
                    _serialize_list_field(body.pool_custom_codes),
                    _serialize_list_field(body.pool_industries),
                    body.source_experiment_id,
                    _json.dumps(metrics_snapshot, ensure_ascii=False),
                    body.notes,
                    _json.dumps(body.labels, ensure_ascii=False),
                    1 if body.is_default else 0,
                ),
            )
            preset_id = cursor.lastrowid
            await conn.commit()
            cursor = await conn.execute(
                "SELECT * FROM parameter_presets WHERE id = ?", (preset_id,)
            )
            row = await cursor.fetchone()
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail="同一策略下参数方案名称不能重复",
        ) from exc
    return {"data": _parameter_preset_to_dict(row)}


@router.get(
    "/parameter-presets/{preset_id}",
    response_model=ApiResponse[ParameterPresetResponse],
)
async def get_parameter_preset(
    preset_id: int,
    user: dict[str, Any] = Depends(require_permission("experiments:read")),
) -> dict[str, Any]:
    async for conn in get_db("experiment"):
        cursor = await conn.execute(
            "SELECT * FROM parameter_presets WHERE id = ? AND user_id = ?",
            (preset_id, user["id"]),
        )
        row = await cursor.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"参数方案不存在: {preset_id}")
    return {"data": _parameter_preset_to_dict(row)}


@router.put(
    "/parameter-presets/{preset_id}",
    response_model=ApiResponse[ParameterPresetResponse],
)
async def update_parameter_preset(
    preset_id: int,
    body: UpdateParameterPresetBody,
    user: dict[str, Any] = Depends(require_permission("experiments:create")),
) -> dict[str, Any]:
    fields = body.model_fields_set
    if not fields:
        raise HTTPException(status_code=422, detail="至少提供一个待更新字段")
    try:
        async for conn in get_db("experiment"):
            cursor = await conn.execute(
                "SELECT * FROM parameter_presets WHERE id = ? AND user_id = ?",
                (preset_id, user["id"]),
            )
            existing = await cursor.fetchone()
            if existing is None:
                raise HTTPException(
                    status_code=404, detail=f"参数方案不存在: {preset_id}"
                )

            if "source_experiment_id" in fields and body.source_experiment_id is not None:
                await _get_source_experiment(
                    conn,
                    body.source_experiment_id,
                    user,
                    strategy_id=existing["strategy_id"],
                    require_completed=True,
                )

            if "mode" in fields and body.mode is not None:
                from backend.dependencies import get_strategy_registry

                metadata = get_strategy_registry().get_metadata(existing["strategy_id"])
                supported_modes = {
                    item.value if hasattr(item, "value") else str(item)
                    for item in metadata.supported_modes
                }
                if body.mode not in supported_modes:
                    raise HTTPException(
                        status_code=422,
                        detail=f"策略不支持运行模式 {body.mode}",
                    )
            if "params" in fields and body.params is not None:
                from backend.dependencies import get_strategy_registry

                is_valid, validation_error = get_strategy_registry().validate_params(
                    existing["strategy_id"], body.params
                )
                if not is_valid:
                    raise HTTPException(
                        status_code=422,
                        detail=f"策略参数无效: {validation_error}",
                    )

            derived_metrics_snapshot: dict[str, Any] | None = None
            if (
                "source_experiment_id" in fields
                and body.source_experiment_id is not None
                and "metrics_snapshot" not in fields
            ):
                derived_metrics_snapshot = await _snapshot_experiment_metrics(
                    conn, body.source_experiment_id
                )

            values: dict[str, Any] = {}
            json_fields = {"params", "metrics_snapshot", "labels"}
            list_fields = {"pool_custom_codes", "pool_industries"}
            for name in fields:
                value = getattr(body, name)
                if name == "name" and value is not None:
                    value = value.strip()
                elif name in json_fields:
                    value = _json.dumps(value or ({} if name != "labels" else []), ensure_ascii=False)
                elif name in list_fields:
                    value = _serialize_list_field(value)
                elif name == "is_default":
                    value = 1 if value else 0
                values[name] = value
            if derived_metrics_snapshot is not None:
                values["metrics_snapshot"] = _json.dumps(
                    derived_metrics_snapshot, ensure_ascii=False
                )

            if values.get("is_default") == 1:
                await conn.execute(
                    """
                    UPDATE parameter_presets SET is_default = 0,
                        updated_at = datetime('now')
                    WHERE user_id = ? AND strategy_id = ? AND id != ?
                    """,
                    (user["id"], existing["strategy_id"], preset_id),
                )
            assignments = ", ".join(f"{name} = ?" for name in values)
            await conn.execute(
                f"""
                UPDATE parameter_presets
                SET {assignments}, updated_at = datetime('now')
                WHERE id = ? AND user_id = ?
                """,
                [*values.values(), preset_id, user["id"]],
            )
            await conn.commit()
            cursor = await conn.execute(
                "SELECT * FROM parameter_presets WHERE id = ?", (preset_id,)
            )
            row = await cursor.fetchone()
    except HTTPException:
        raise
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail="同一策略下参数方案名称不能重复",
        ) from exc
    return {"data": _parameter_preset_to_dict(row)}


@router.delete("/parameter-presets/{preset_id}")
async def delete_parameter_preset(
    preset_id: int,
    user: dict[str, Any] = Depends(require_permission("experiments:create")),
) -> dict[str, Any]:
    async for conn in get_db("experiment"):
        cursor = await conn.execute(
            "DELETE FROM parameter_presets WHERE id = ? AND user_id = ?",
            (preset_id, user["id"]),
        )
        await conn.commit()
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail=f"参数方案不存在: {preset_id}")
    return {"data": {"deleted": True, "parameter_preset_id": preset_id}}


# ═══════════════════════════════════════════════════════════════════════════
# POST / — 创建实验
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/", response_model=ApiResponse[IdJobResponse])
async def create_experiment(
    body: CreateExperimentBody,
    user: dict[str, Any] = Depends(require_permission("experiments:create")),
) -> dict[str, Any]:
    """创建实验并提交后台任务。返回 job_id。"""
    research_trust = await _require_pit_submission(
        pool_id=body.pool_preset,
        train_start=body.train_start,
        test_start=body.test_start,
        test_end=body.test_end,
        data_access_policy=body.data_access_policy,
        research_trust_profile=body.research_trust_profile,
        purpose="research",
    )
    if research_trust is not None and body.pool_industries:
        research_trust.setdefault("warnings", []).extend(
            [
                "industry_filter_uses_current_classification",
                "historical_industry_neutralization_not_proven",
            ]
        )
    # 校验策略是否存在
    from backend.dependencies import get_strategy_registry
    registry = get_strategy_registry()
    try:
        strategy_meta = registry.get_metadata(body.strategy_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"策略不存在: {body.strategy_id}")
    supported_modes = {
        item.value if hasattr(item, "value") else str(item)
        for item in strategy_meta.supported_modes
    }
    if body.mode not in supported_modes:
        raise HTTPException(
            status_code=422,
            detail=f"策略不支持运行模式 {body.mode}",
        )
    if _require_training_window(strategy_meta) and not (
        body.train_start and body.train_end
    ):
        raise HTTPException(
            status_code=422,
            detail="一次训练模型必须提供完整训练窗口",
        )
    canonical_params = {
        item.name: item.default
        for item in strategy_meta.params
        if item.default is not None
    }
    canonical_params.update(body.params)
    is_valid, validation_error = registry.validate_params(
        body.strategy_id,
        canonical_params,
    )
    if not is_valid:
        raise HTTPException(status_code=422, detail=f"策略参数无效: {validation_error}")

    params_str = _json.dumps(canonical_params, ensure_ascii=False)
    params_hash = _hash_params(canonical_params)
    pool_custom_codes = _serialize_list_field(body.pool_custom_codes)
    pool_industries = _serialize_list_field(body.pool_industries)
    run_spec = _json.dumps(
        {
            "strategy_id": body.strategy_id,
            "pool_preset": body.pool_preset,
            "pool_custom_codes": body.pool_custom_codes,
            "pool_industries": body.pool_industries,
            "train_start": body.train_start,
            "train_end": body.train_end,
            "test_start": body.test_start,
            "test_end": body.test_end,
            "params": canonical_params,
            "mode": body.mode,
            "data_access_policy": body.data_access_policy,
            "research_trust_profile": body.research_trust_profile,
            "research_trust": research_trust,
            "source_experiment_id": body.source_experiment_id,
        },
        ensure_ascii=False,
        sort_keys=True,
    )

    try:
        async for conn in get_db("experiment"):
            if body.source_experiment_id is not None:
                await _get_source_experiment(
                    conn,
                    body.source_experiment_id,
                    user,
                    strategy_id=body.strategy_id,
                )
            cursor = await conn.execute(
                """
                INSERT INTO experiments
                    (user_id, name, strategy_id, strategy_category,
                     pool_preset, pool_custom_codes, pool_industries,
                     train_start, train_end, test_start, test_end,
                     params, params_hash, mode, requires_training, retrain_frequency,
                     status, progress_pct, progress_message, run_spec,
                     source_experiment_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user["id"],
                    body.name,
                    body.strategy_id,
                    strategy_meta.category.value if hasattr(strategy_meta.category, "value") else str(strategy_meta.category),
                    body.pool_preset,
                    pool_custom_codes,
                    pool_industries,
                    body.train_start,
                    body.train_end,
                    body.test_start,
                    body.test_end,
                    params_str,
                    params_hash,
                    body.mode,
                    1 if strategy_meta.requires_training else 0,
                    strategy_meta.retrain_frequency.value if hasattr(strategy_meta.retrain_frequency, "value") else str(strategy_meta.retrain_frequency),
                    "pending",
                    0,
                    "等待执行",
                    run_spec,
                    body.source_experiment_id,
                ),
            )
            await conn.commit()
            experiment_id = cursor.lastrowid
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"创建实验失败: {e}")

    # 提交后台任务
    broker = get_job_broker()
    try:
        # FIXED: reviewer issue #4 — submit() → submit_job(), 参数名匹配
        job_id = await broker.submit_job(
            job_type="backtest",
            params={
                "experiment_id": experiment_id,
                "strategy_id": body.strategy_id,
                "params": canonical_params,
                "pool_preset": body.pool_preset,
                "pool_custom_codes": pool_custom_codes,
                "pool_industries": pool_industries,
                "train_start": body.train_start,
                "train_end": body.train_end,
                "test_start": body.test_start,
                "test_end": body.test_end,
                "mode": body.mode,
                "data_access_policy": body.data_access_policy,
                "user_id": user["id"],
                "source_experiment_id": body.source_experiment_id,
            },
            user_id=user["id"],
            display_name=body.name,
            resource_type="experiment",
            resource_id=experiment_id,
        )
    except Exception:
        # 任务提交失败但不回滚实验记录（可重试）
        job_id = f"exp-{experiment_id}"
        try:
            async for conn in get_db("experiment"):
                await conn.execute(
                    "UPDATE experiments SET status = 'failed', error_log = ? WHERE id = ?",
                    ("任务提交失败", experiment_id),
                )
                await conn.commit()
        except Exception:
            pass

    return {"data": {"experiment_id": experiment_id, "job_id": job_id}}


# ═══════════════════════════════════════════════════════════════════════════
# GET /recovery — 精确查找前端提交恢复候选
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/recovery")
async def find_submission_recovery_candidates(
    name: str = Query(..., min_length=1, max_length=200),
    strategy_id: str = Query(..., min_length=1, max_length=100),
    user: dict[str, Any] = Depends(require_permission("experiments:read")),
) -> dict[str, Any]:
    """Return exact-name, current-owner candidates for read-only recovery.

    The frontend tuning runner uses this after persisting a deterministic
    submission intent.  If the browser POST committed but the runner crashed
    before writing the returned ID to its checkpoint, the existing experiment
    or sweep can be recovered without issuing another write.
    """
    async for conn in get_db("experiment"):
        cursor = await conn.execute(
            """
            SELECT id, name, strategy_id, pool_preset, pool_custom_codes,
                   pool_industries, train_start, train_end, test_start,
                   test_end, params, params_hash, mode, status, created_at,
                   source_experiment_id, run_spec
            FROM experiments
            WHERE user_id=? AND name=? AND strategy_id=?
            ORDER BY id
            """,
            (user["id"], name, strategy_id),
        )
        experiment_rows = await cursor.fetchall()
        cursor = await conn.execute(
            """
            SELECT ps.id, ps.name, ps.strategy_id, ps.sweep_config,
                   ps.selection_start, ps.selection_end,
                   ps.locked_test_start, ps.locked_test_end,
                   ps.research_trust, ps.total_experiments, ps.status,
                   ps.created_at, ps.promoted_experiment_id,
                   ps.promotion_source_experiment_id,
                   (
                       SELECT e.run_spec
                       FROM sweep_experiments se
                       JOIN experiments e ON e.id = se.experiment_id
                       WHERE se.sweep_id = ps.id
                       ORDER BY e.id
                       LIMIT 1
                   ) AS member_run_spec,
                   (
                       SELECT e.source_experiment_id
                       FROM sweep_experiments se
                       JOIN experiments e ON e.id = se.experiment_id
                       WHERE se.sweep_id = ps.id
                       ORDER BY e.id
                       LIMIT 1
                   ) AS source_experiment_id
            FROM param_sweeps ps
            WHERE ps.user_id=? AND ps.name=? AND ps.strategy_id=?
            ORDER BY ps.id
            """,
            (user["id"], name, strategy_id),
        )
        sweep_rows = await cursor.fetchall()

    experiments: list[dict[str, Any]] = []
    for row in experiment_rows:
        candidate = _row_to_dict(row)
        candidate["pool_custom_codes"] = _deserialize_list_field(
            candidate.get("pool_custom_codes")
        )
        candidate["pool_industries"] = _deserialize_list_field(
            candidate.get("pool_industries")
        )
        candidate["params"] = _json.loads(candidate["params"] or "{}")
        candidate["data_access_policy"] = _data_access_policy_from_run_spec(
            candidate.pop("run_spec", None)
        )
        experiments.append(candidate)

    sweeps: list[dict[str, Any]] = []
    for row in sweep_rows:
        candidate = _row_to_dict(row)
        candidate["sweep_config"] = _json.loads(
            candidate["sweep_config"] or "{}"
        )
        candidate["data_access_policy"] = _data_access_policy_from_run_spec(
            candidate.pop("member_run_spec", None)
        )
        sweeps.append(candidate)

    return {"data": {"experiments": experiments, "sweeps": sweeps}}


# ═══════════════════════════════════════════════════════════════════════════
# GET /picker — 部署选择器数据（必须在 /{id} 之前定义）
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/picker", response_model=ApiResponse[list[ExperimentResponse]])
async def experiment_picker(
    strategy_id: str | None = Query(None),
    starred_only: bool = Query(False),
    sort: str = Query("sharpe", description="sharpe|return|drawdown|date"),
    limit: int = Query(20, ge=1, le=100),
    user: dict[str, Any] = Depends(require_permission("experiments:read")),
) -> dict[str, Any]:
    """部署选择器：获取可用于部署的实验列表。"""
    conditions: list[str] = ["e.status = 'completed'"]
    params: list[Any] = []

    if strategy_id:
        conditions.append("e.strategy_id = ?")
        params.append(strategy_id)

    if starred_only:
        conditions.append("e.is_starred = 1")

    if not user.get("is_admin"):
        conditions.append("e.user_id = ?")
        params.append(user["id"])

    sort_map = {
        "sharpe": (
            "m.sharpe_ratio IS NULL ASC, m.sharpe_ratio DESC, e.id DESC"
        ),
        "return": (
            "m.annual_return IS NULL ASC, m.annual_return DESC, e.id DESC"
        ),
        "drawdown": (
            "m.max_drawdown IS NULL ASC, m.max_drawdown DESC, e.id DESC"
        ),
        "date": "e.created_at DESC, e.id DESC",
    }
    order_by = sort_map.get(sort, sort_map["sharpe"])

    where = "WHERE " + " AND ".join(conditions)

    try:
        async for conn in get_db("experiment"):
            cursor = await conn.execute(
                f"""
                SELECT e.id, e.name, e.strategy_id, e.is_starred, e.labels,
                       e.params, e.test_start, e.test_end, e.created_at,
                       m.sharpe_ratio, m.annual_return, m.max_drawdown,
                       m.win_rate, m.total_trades,
                       rm.schema_version AS manifest_schema_version,
                       rm.manifest_json, rm.manifest_hash
                FROM experiments e
                LEFT JOIN experiment_metrics m ON e.id = m.experiment_id
                JOIN research_run_manifests rm ON rm.experiment_id = e.id
                {where}
                ORDER BY {order_by}
                LIMIT ?
                """,
                params + [min(max(limit * 20, limit), 1000)],
            )
            rows = await cursor.fetchall()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询选择器数据失败: {e}")

    items: list[dict[str, Any]] = []
    for row in rows:
        eligibility = assess_experiment_eligibility(
            experiment_id=int(row["id"]),
            strategy_id=str(row["strategy_id"]),
            manifest_json=row["manifest_json"],
            manifest_hash=row["manifest_hash"],
            schema_version=row["manifest_schema_version"],
        )
        if not eligibility.eligible:
            continue
        items.append({
            "id": row["id"],
            "name": row["name"],
            "strategy_id": row["strategy_id"],
            "is_starred": bool(row["is_starred"]),
            "labels": _json.loads(row["labels"]) if row["labels"] else [],
            "params": _json.loads(row["params"]) if row["params"] else {},
            "test_start": row["test_start"],
            "test_end": row["test_end"],
            "created_at": serialize_utc_timestamp(row["created_at"]),
            "sharpe_ratio": row["sharpe_ratio"],
            "annual_return": row["annual_return"],
            "max_drawdown": row["max_drawdown"],
            "win_rate": row["win_rate"],
            "total_trades": row["total_trades"],
            **eligibility.public_dict(),
        })
        if len(items) >= limit:
            break

    return {"data": items}


# ═══════════════════════════════════════════════════════════════════════════
# GET /{id}/export — 完整研究证据
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/{experiment_id}/export")
async def export_experiment_research_evidence(
    experiment_id: int,
    format: Literal["json", "csv"] = Query(  # noqa: A002
        "json",
        description="json 为流式 JSON；csv 为多表 CSV ZIP",
    ),
    user: dict[str, Any] = Depends(
        require_permission("experiments:read")
    ),
) -> StreamingResponse:
    """导出完成实验的配置、结果、清单、血缘与风险证据。"""
    from backend.config import settings

    db_path = settings.abs_path(settings.EXPERIMENT_DB)
    try:
        context = await prepare_research_evidence(
            db_path,
            experiment_id,
            user,
        )
        generated_stamp = str(context["generated_at"]).replace(
            "-", ""
        ).replace(":", "")[:15]
        basename = (
            f"research-evidence-experiment-{experiment_id}-{generated_stamp}"
        )
        headers = {
            "Cache-Control": "no-store",
            "Content-Disposition": (
                f'attachment; filename="{basename}.'
                f'{"json" if format == "json" else "zip"}"'
            ),
            "X-Content-Type-Options": "nosniff",
        }
        if format == "json":
            return StreamingResponse(
                stream_json_evidence(
                    db_path,
                    experiment_id,
                    context,
                ),
                media_type=JSON_MEDIA_TYPE,
                headers=headers,
            )
        archive = await build_csv_zip_evidence(
            db_path,
            experiment_id,
            context,
        )
        return StreamingResponse(
            stream_binary_file(archive),
            media_type=ZIP_MEDIA_TYPE,
            headers=headers,
        )
    except ResearchEvidenceExportError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={
                "code": exc.code,
                "message": exc.message,
            },
        ) from exc


# ═══════════════════════════════════════════════════════════════════════════
# GET /{id} — 实验详情
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/{experiment_id}", response_model=ApiResponse[ExperimentResponse])
async def get_experiment_detail(
    experiment_id: int,
    user: dict[str, Any] = Depends(require_permission("experiments:read")),
) -> dict[str, Any]:
    """获取实验详情（含进度、状态、指标摘要）。"""
    try:
        async for conn in get_db("experiment"):
            # FIXED: reviewer issue #11 — 增加 user_id 过滤
            query = """
                SELECT e.*, m.*
                FROM experiments e
                LEFT JOIN experiment_metrics m ON e.id = m.experiment_id
                WHERE e.id = ?
            """
            query_params: list[Any] = [experiment_id]
            if not user.get("is_admin"):
                query += " AND e.user_id = ?"
                query_params.append(user["id"])

            cursor = await conn.execute(query, query_params)
            row = await cursor.fetchone()
            risk_summaries = await _research_risk_summaries(
                conn,
                [experiment_id] if row is not None else [],
                user,
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询实验失败: {e}")

    if row is None:
        raise HTTPException(status_code=404, detail=f"实验不存在: {experiment_id}")

    result = _row_to_dict(row)
    result["labels"] = _json.loads(result["labels"]) if result.get("labels") else []
    result["params"] = _json.loads(result["params"]) if result.get("params") else {}
    result["pool_custom_codes"] = _deserialize_list_field(
        result.get("pool_custom_codes")
    )
    result["pool_industries"] = _deserialize_list_field(
        result.get("pool_industries")
    )
    result["is_starred"] = bool(result.get("is_starred", 0))
    result["data_access_policy"] = _data_access_policy_from_run_spec(
        result.get("run_spec")
    )
    result["research_trust"] = _research_trust_from_run_spec(
        result.get("run_spec")
    )
    result.pop("run_spec", None)
    result["research_risk_summary"] = risk_summaries.get(
        experiment_id,
        research_risk_summary(
            manifest_json=None,
            manifest_hash=None,
            schema_version=None,
        ),
    )

    return {"data": result}


# ═══════════════════════════════════════════════════════════════════════════
# DELETE /{id} — 删除实验
# ═══════════════════════════════════════════════════════════════════════════

@router.delete("/{experiment_id}")
async def delete_experiment(
    experiment_id: int,
    user: dict[str, Any] = Depends(require_permission("experiments:delete")),
) -> dict[str, Any]:
    """删除实验及所有关联数据（级联删除）。"""
    try:
        async for conn in get_db("experiment"):
            # 检查存在 — FIXED: reviewer issue #11 — 增加 user_id 过滤
            if not user.get("is_admin"):
                cursor = await conn.execute(
                    "SELECT id FROM experiments WHERE id = ? AND user_id = ?",
                    (experiment_id, user["id"]),
                )
            else:
                cursor = await conn.execute(
                    "SELECT id FROM experiments WHERE id = ?",
                    (experiment_id,),
                )
            if await cursor.fetchone() is None:
                raise HTTPException(status_code=404, detail=f"实验不存在: {experiment_id}")

            await conn.execute("PRAGMA foreign_keys=ON")
            await conn.execute("DELETE FROM experiments WHERE id = ?", (experiment_id,))
            await conn.commit()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"删除实验失败: {e}")

    return {"data": {"deleted": True, "experiment_id": experiment_id}}


# ═══════════════════════════════════════════════════════════════════════════
# GET /{id}/metrics — 36项指标
# ═══════════════════════════════════════════════════════════════════════════

@router.get(
    "/{experiment_id}/metrics",
    response_model=ApiResponse[MetricsResponse | None],
)
async def get_experiment_metrics(
    experiment_id: int,
    user: dict[str, Any] = Depends(require_permission("experiments:read")),
) -> dict[str, Any]:
    """获取实验的36项指标。"""
    try:
        async for conn in get_db("experiment"):
            # FIXED: reviewer issue #11 — 验证实验归属
            if not user.get("is_admin"):
                cursor = await conn.execute(
                    "SELECT id FROM experiments WHERE id = ? AND user_id = ?",
                    (experiment_id, user["id"]),
                )
            else:
                cursor = await conn.execute("SELECT id FROM experiments WHERE id = ?", (experiment_id,))
            if await cursor.fetchone() is None:
                raise HTTPException(status_code=404, detail=f"实验不存在: {experiment_id}")

            cursor = await conn.execute(
                "SELECT * FROM experiment_metrics WHERE experiment_id = ?",
                (experiment_id,),
            )
            row = await cursor.fetchone()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询指标失败: {e}")

    if row is None:
        return {"data": None, "detail": "该实验尚未生成指标"}

    metrics = _row_to_dict(row)
    # 移除内部字段
    metrics.pop("id", None)
    metrics.pop("created_at", None)
    metrics["annualized_return"] = metrics.get("annual_return")
    metrics["annualized_volatility"] = metrics.get("volatility")

    return {"data": metrics}


# ═══════════════════════════════════════════════════════════════════════════
# GET /{id}/equity — 净值曲线
# ═══════════════════════════════════════════════════════════════════════════

@router.get(
    "/{experiment_id}/equity",
    response_model=ApiResponse[list[EquityPointResponse]],
)
async def get_equity_curve(
    experiment_id: int,
    resolution: str = Query("daily", description="daily|weekly|monthly"),
    user: dict[str, Any] = Depends(require_permission("experiments:read")),
) -> dict[str, Any]:
    """获取净值曲线数据。"""
    try:
        async for conn in get_db("experiment"):
            # FIXED: reviewer issue #11 — 验证实验归属
            if not user.get("is_admin"):
                cursor = await conn.execute(
                    "SELECT id FROM experiments WHERE id = ? AND user_id = ?",
                    (experiment_id, user["id"]),
                )
            else:
                cursor = await conn.execute("SELECT id FROM experiments WHERE id = ?", (experiment_id,))
            if await cursor.fetchone() is None:
                raise HTTPException(status_code=404, detail=f"实验不存在: {experiment_id}")

            if resolution == "daily":
                cursor = await conn.execute(
                    "SELECT date, equity, benchmark, daily_return, drawdown FROM equity_curve WHERE experiment_id = ? ORDER BY date",
                    (experiment_id,),
                )
            elif resolution == "weekly":
                cursor = await conn.execute(
                    """
                    SELECT date, equity, benchmark, daily_return, drawdown
                    FROM equity_curve
                    WHERE experiment_id = ?
                      AND date IN (SELECT date FROM equity_curve WHERE experiment_id = ? AND strftime('%w', date) = '5')
                    ORDER BY date
                    """,
                    (experiment_id, experiment_id),
                )
            elif resolution == "monthly":
                cursor = await conn.execute(
                    """
                    SELECT date, equity, benchmark, daily_return, drawdown
                    FROM equity_curve
                    WHERE experiment_id = ?
                      AND date IN (
                        SELECT date FROM equity_curve
                        WHERE experiment_id = ?
                        GROUP BY substr(date, 1, 7)
                        HAVING date = MAX(date)
                      )
                    ORDER BY date
                    """,
                    (experiment_id, experiment_id),
                )
            else:
                raise HTTPException(status_code=400, detail=f"不支持的分辨率: {resolution}")

            rows = await cursor.fetchall()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询净值曲线失败: {e}")

    return {"data": [_row_to_dict(r) for r in rows]}


# ═══════════════════════════════════════════════════════════════════════════
# GET /{id}/trades — 成交明细
# ═══════════════════════════════════════════════════════════════════════════

@router.get(
    "/{experiment_id}/trades",
    response_model=ApiResponse[Page[TradeResponse]],
)
async def get_trades(
    experiment_id: int,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=500),
    user: dict[str, Any] = Depends(require_permission("experiments:read")),
) -> dict[str, Any]:
    """获取成交明细，支持分页。"""
    offset = (page - 1) * limit

    try:
        async for conn in get_db("experiment"):
            # FIXED: reviewer issue #11 — 验证实验归属
            if not user.get("is_admin"):
                cursor = await conn.execute(
                    "SELECT id FROM experiments WHERE id = ? AND user_id = ?",
                    (experiment_id, user["id"]),
                )
            else:
                cursor = await conn.execute("SELECT id FROM experiments WHERE id = ?", (experiment_id,))
            if await cursor.fetchone() is None:
                raise HTTPException(status_code=404, detail=f"实验不存在: {experiment_id}")

            cursor = await conn.execute(
                "SELECT COUNT(*) as cnt FROM trade_log WHERE experiment_id = ?",
                (experiment_id,),
            )
            total_row = await cursor.fetchone()
            total = total_row["cnt"] if total_row else 0

            cursor = await conn.execute(
                "SELECT * FROM trade_log WHERE experiment_id = ? ORDER BY date, id LIMIT ? OFFSET ?",
                (experiment_id, limit, offset),
            )
            rows = await cursor.fetchall()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询成交明细失败: {e}")

    return {
        "data": {
            "items": [_row_to_dict(r) for r in rows],
            "total": total,
            "page": page,
            "limit": limit,
        }
    }


# ═══════════════════════════════════════════════════════════════════════════
# GET /{id}/models — 模型产物列表
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/{experiment_id}/models")
async def get_model_artifacts(
    experiment_id: int,
    user: dict[str, Any] = Depends(require_permission("experiments:read")),
) -> dict[str, Any]:
    """获取实验的模型产物列表。"""
    try:
        async for conn in get_db("experiment"):
            # FIXED: reviewer issue #11 — 验证实验归属
            if not user.get("is_admin"):
                cursor = await conn.execute(
                    "SELECT id FROM experiments WHERE id = ? AND user_id = ?",
                    (experiment_id, user["id"]),
                )
            else:
                cursor = await conn.execute("SELECT id FROM experiments WHERE id = ?", (experiment_id,))
            if await cursor.fetchone() is None:
                raise HTTPException(status_code=404, detail=f"实验不存在: {experiment_id}")

            cursor = await conn.execute(
                "SELECT * FROM model_artifacts WHERE experiment_id = ? ORDER BY model_version DESC",
                (experiment_id,),
            )
            rows = await cursor.fetchall()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询模型产物失败: {e}")

    items: list[dict[str, Any]] = []
    for row in rows:
        d = redact_model_storage_paths(_row_to_dict(row))
        d["train_metrics"] = _json.loads(d["train_metrics"]) if d.get("train_metrics") else {}
        d["feature_importance"] = _json.loads(d["feature_importance"]) if d.get("feature_importance") else {}
        d["is_latest"] = bool(d.get("is_latest"))
        items.append(d)

    return {"data": items}


# ═══════════════════════════════════════════════════════════════════════════
# PUT /{id}/star — 切换星标
# ═══════════════════════════════════════════════════════════════════════════

@router.put("/{experiment_id}/star")
async def toggle_star(
    experiment_id: int,
    body: StarBody,
    user: dict[str, Any] = Depends(require_permission("experiments:read")),
) -> dict[str, Any]:
    """切换实验星标状态。"""
    try:
        async for conn in get_db("experiment"):
            # FIXED: reviewer issue #11 — 增加 user_id 过滤
            if not user.get("is_admin"):
                cursor = await conn.execute(
                    "SELECT id FROM experiments WHERE id = ? AND user_id = ?",
                    (experiment_id, user["id"]),
                )
            else:
                cursor = await conn.execute("SELECT id FROM experiments WHERE id = ?", (experiment_id,))
            if await cursor.fetchone() is None:
                raise HTTPException(status_code=404, detail=f"实验不存在: {experiment_id}")

            await conn.execute(
                "UPDATE experiments SET is_starred = ? WHERE id = ?",
                (1 if body.is_starred else 0, experiment_id),
            )
            await conn.commit()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"更新星标失败: {e}")

    return {"data": {"experiment_id": experiment_id, "is_starred": body.is_starred}}


# ═══════════════════════════════════════════════════════════════════════════
# PUT /{id}/labels — 设置标签
# ═══════════════════════════════════════════════════════════════════════════

@router.put("/{experiment_id}/labels")
async def set_labels(
    experiment_id: int,
    body: LabelsBody,
    user: dict[str, Any] = Depends(require_permission("experiments:read")),
) -> dict[str, Any]:
    """设置实验标签。"""
    labels_str = _json.dumps(body.labels, ensure_ascii=False)

    try:
        async for conn in get_db("experiment"):
            # FIXED: reviewer issue #11 — 增加 user_id 过滤
            if not user.get("is_admin"):
                cursor = await conn.execute(
                    "SELECT id FROM experiments WHERE id = ? AND user_id = ?",
                    (experiment_id, user["id"]),
                )
            else:
                cursor = await conn.execute("SELECT id FROM experiments WHERE id = ?", (experiment_id,))
            if await cursor.fetchone() is None:
                raise HTTPException(status_code=404, detail=f"实验不存在: {experiment_id}")

            await conn.execute(
                "UPDATE experiments SET labels = ? WHERE id = ?",
                (labels_str, experiment_id),
            )
            await conn.commit()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"更新标签失败: {e}")

    return {"data": {"experiment_id": experiment_id, "labels": body.labels}}


# MOVED: /picker 已移到 /{id} 之前定义，避免路由冲突


# ═══════════════════════════════════════════════════════════════════════════
# GET /sweep/{id} — 扫描结果（必须在 /{id} 之前定义）
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/sweep/{sweep_id}")
async def get_sweep_result(
    sweep_id: int,
    user: dict[str, Any] = Depends(require_permission("experiments:read")),
) -> dict[str, Any]:
    """获取参数扫描的汇总结果。"""
    try:
        async for conn in get_db("experiment"):
            sweep_query = "SELECT * FROM param_sweeps WHERE id = ?"
            sweep_params: list[Any] = [sweep_id]
            if not user.get("is_admin"):
                sweep_query += " AND user_id = ?"
                sweep_params.append(user["id"])
            cursor = await conn.execute(sweep_query, sweep_params)
            sweep_row = await cursor.fetchone()
            if sweep_row is None:
                raise HTTPException(status_code=404, detail=f"参数扫描不存在: {sweep_id}")

            prior_job_sql, manifest_sql = await _sweep_repair_evidence_sql(conn)
            cursor = await conn.execute(
                f"""
                SELECT e.id, e.name, e.params, e.status, e.run_spec, e.error_log,
                       {prior_job_sql} AS prior_transient_job_failure,
                       {manifest_sql} AS has_manifest,
                       m.sharpe_ratio, m.annual_return, m.max_drawdown, m.win_rate
                FROM sweep_experiments se
                JOIN experiments e ON se.experiment_id = e.id
                LEFT JOIN experiment_metrics m ON e.id = m.experiment_id
                WHERE se.sweep_id = ?
                ORDER BY m.sharpe_ratio DESC
                """,
                (sweep_id,),
            )
            exp_rows = await cursor.fetchall()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询扫描结果失败: {e}")

    experiments: list[dict[str, Any]] = []
    repairable_experiment_ids: list[int] = []
    for row in exp_rows:
        repairable = (
            row["status"] == "failed"
            and _is_repairable_sweep_member(
                row["error_log"],
                prior_transient_job_failure=bool(
                    row["prior_transient_job_failure"]
                ),
            )
        )
        if repairable:
            repairable_experiment_ids.append(int(row["id"]))
        experiments.append({
            "id": row["id"],
            "name": row["name"],
            "params": _json.loads(row["params"]) if row["params"] else {},
            "status": row["status"],
            "repairable": repairable,
            "repair_mode": (
                "replace" if repairable and row["has_manifest"] else
                "reset" if repairable else None
            ),
            "selection_metrics": {
                "sharpe_ratio": row["sharpe_ratio"],
                "annual_return": row["annual_return"],
                "max_drawdown": row["max_drawdown"],
                "win_rate": row["win_rate"],
            },
        })

    sweep = _row_to_dict(sweep_row)
    sweep["sweep_config"] = (
        _json.loads(sweep["sweep_config"])
        if sweep.get("sweep_config")
        else {}
    )
    sweep["data_access_policy"] = (
        _data_access_policy_from_run_spec(exp_rows[0]["run_spec"])
        if exp_rows
        else "allow_fetch"
    )
    return {
        "data": {
            "sweep": sweep,
            "experiments": experiments,
            "repairable_experiment_ids": repairable_experiment_ids,
        }
    }


# ═══════════════════════════════════════════════════════════════════════════
# POST /sweep/{id}/repair — 仅恢复 SQLite 瞬态写冲突失败成员
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/sweep/{sweep_id}/repair")
async def repair_param_sweep(
    sweep_id: int,
    user: dict[str, Any] = Depends(require_permission("experiments:sweep")),
) -> dict[str, Any]:
    """Requeue only members that failed because SQLite was temporarily busy."""
    try:
        async for conn in get_db("experiment"):
            sweep_query = "SELECT id, user_id, name FROM param_sweeps WHERE id=?"
            sweep_params: list[Any] = [sweep_id]
            if not user.get("is_admin"):
                sweep_query += " AND user_id=?"
                sweep_params.append(user["id"])
            cursor = await conn.execute(sweep_query, sweep_params)
            sweep = await cursor.fetchone()
            if sweep is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"参数扫描不存在: {sweep_id}",
                )
            prior_job_sql, manifest_sql = await _sweep_repair_evidence_sql(conn)
            cursor = await conn.execute(
                f"""
                SELECT e.id, e.name, e.error_log, e.run_spec,
                       {prior_job_sql} AS prior_transient_job_failure,
                       {manifest_sql} AS has_manifest
                FROM sweep_experiments se
                JOIN experiments e ON e.id=se.experiment_id
                WHERE se.sweep_id=? AND e.status='failed'
                ORDER BY e.id
                """,
                (sweep_id,),
            )
            failed_rows = await cursor.fetchall()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"查询可恢复成员失败: {exc}")

    repairable = [
        row for row in failed_rows
        if _is_repairable_sweep_member(
            row["error_log"],
            prior_transient_job_failure=bool(
                row["prior_transient_job_failure"]
            ),
        )
    ]
    if not repairable:
        raise HTTPException(status_code=409, detail="没有可安全恢复的瞬态失败成员")

    submissions: list[dict[str, Any]] = []
    experiment_ids: list[int] = []
    reset_experiment_ids: list[int] = []
    replace_experiment_ids: list[int] = []
    for row in repairable:
        run_spec = _json.loads(row["run_spec"]) if row["run_spec"] else {}
        experiment_id = int(row["id"])
        experiment_ids.append(experiment_id)
        if row["has_manifest"]:
            replace_experiment_ids.append(experiment_id)
        else:
            reset_experiment_ids.append(experiment_id)
        submissions.append(
            {
                "job_type": "backtest",
                "params": {
                    "experiment_id": experiment_id,
                    "sweep_id": sweep_id,
                    "pool_preset": run_spec.get("pool_preset"),
                    "pool_custom_codes": run_spec.get("pool_custom_codes"),
                },
                "user_id": int(sweep["user_id"]),
                "display_name": f"{sweep['name']} · 恢复实验 #{experiment_id}",
                "resource_type": "experiment",
                "resource_id": experiment_id,
            }
        )

    broker = get_job_broker()
    if not hasattr(broker, "submit_jobs_batch"):
        raise HTTPException(status_code=503, detail="当前任务队列不支持原子恢复")
    try:
        job_ids = await broker.submit_jobs_batch(
            submissions,
            sweep_id=sweep_id,
            reset_experiment_ids=reset_experiment_ids,
            replace_experiment_ids=replace_experiment_ids,
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"恢复扫描成员失败: {exc}")

    replacement_experiment_ids: dict[str, int] = {}
    for original_id, job_id in zip(experiment_ids, job_ids):
        job = await broker.get_job_status(job_id)
        target_id = int((job or {}).get("params", {}).get("experiment_id", 0))
        if target_id and target_id != original_id:
            replacement_experiment_ids[str(original_id)] = target_id

    return {
        "data": {
            "sweep_id": sweep_id,
            "repaired_experiment_ids": experiment_ids,
            "replacement_experiment_ids": replacement_experiment_ids,
            "job_ids": job_ids,
            "status": "running",
        }
    }


# ═══════════════════════════════════════════════════════════════════════════
# POST /sweep/{id}/promote — 显式晋升一个成员到锁定测试
# ═══════════════════════════════════════════════════════════════════════════


async def _locked_test_research_binding(
    *,
    member: Mapping[str, Any],
    sweep: Mapping[str, Any],
) -> tuple[ResearchTrustProfile, dict[str, Any] | None]:
    """Bind a promoted test to its own window and the member's generation."""

    try:
        run_spec = _json.loads(member.get("run_spec") or "{}")
    except (_json.JSONDecodeError, TypeError) as exc:
        raise HTTPException(
            status_code=409,
            detail="扫描成员的运行清单不可读取",
        ) from exc
    if not isinstance(run_spec, dict):
        raise HTTPException(status_code=409, detail="扫描成员的运行清单无效")
    profile = str(
        run_spec.get("research_trust_profile") or "governed_production_pit"
    )
    if profile not in {
        "governed_production_pit",
        "tushare_research_trusted",
    }:
        raise HTTPException(status_code=409, detail="扫描成员的研究信任档案无效")
    typed_profile: ResearchTrustProfile = (
        "tushare_research_trusted"
        if profile == "tushare_research_trusted"
        else "governed_production_pit"
    )
    generation_id: str | None = None
    if typed_profile == "tushare_research_trusted":
        trust = run_spec.get("research_trust")
        runtime_binding = (
            trust.get("runtime_binding") if isinstance(trust, dict) else None
        )
        generation_id = (
            str(runtime_binding.get("generation_id") or "")
            if isinstance(runtime_binding, dict)
            else ""
        ) or None
        if generation_id is None:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "research_generation_binding_missing",
                    "message": "扫描成员缺少不可变研究数据代，不能晋级锁定测试",
                },
            )
    assessment = await _require_pit_submission(
        pool_id=str(member.get("pool_preset") or ""),
        train_start=member.get("train_start"),
        test_start=str(sweep["locked_test_start"]),
        test_end=str(sweep["locked_test_end"]),
        data_access_policy=_data_access_policy_from_run_spec(
            member.get("run_spec")
        ),
        research_trust_profile=typed_profile,
        purpose="tuning",
        research_generation_id=generation_id,
    )
    return typed_profile, assessment


@router.post("/sweep/{sweep_id}/promote")
async def promote_sweep_experiment(
    sweep_id: int,
    body: PromoteSweepBody,
    user: dict[str, Any] = Depends(require_permission("experiments:sweep")),
) -> dict[str, Any]:
    """Create exactly one locked-test experiment from a chosen sweep member."""
    promotion_id: int | None = None
    created = False
    strategy_id = ""
    params: dict[str, Any] = {}
    display_name = ""
    locked_trust_profile: ResearchTrustProfile = "governed_production_pit"
    locked_trust_assessment: dict[str, Any] | None = None

    # Keep the potentially large immutable generation read outside the write
    # transaction.  The transaction below repeats ownership, status and
    # single-promotion checks before it commits the bound experiment.
    try:
        async for preflight_conn in get_db("experiment"):
            sweep_query = "SELECT * FROM param_sweeps WHERE id = ?"
            sweep_params: list[Any] = [sweep_id]
            if not user.get("is_admin"):
                sweep_query += " AND user_id = ?"
                sweep_params.append(user["id"])
            sweep_row = await (
                await preflight_conn.execute(sweep_query, sweep_params)
            ).fetchone()
            if sweep_row is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"参数扫描不存在或无权访问: {sweep_id}",
                )
            if not sweep_row["locked_test_start"] or not sweep_row["locked_test_end"]:
                raise HTTPException(
                    status_code=422,
                    detail="旧版未锁定扫描不能晋升到锁定测试",
                )
            if sweep_row["promoted_experiment_id"] is not None:
                if sweep_row["promotion_source_experiment_id"] != body.experiment_id:
                    raise HTTPException(
                        status_code=409,
                        detail="该扫描已从另一个成员创建锁定测试实验",
                    )
                return {
                    "data": {
                        "sweep_id": sweep_id,
                        "source_experiment_id": body.experiment_id,
                        "experiment_id": int(sweep_row["promoted_experiment_id"]),
                        "created": False,
                        "research_trust": "locked_test",
                    }
                }
            member_row = await (
                await preflight_conn.execute(
                    """
                    SELECT e.* FROM sweep_experiments se
                    JOIN experiments e ON e.id=se.experiment_id
                    WHERE se.sweep_id=? AND se.experiment_id=? AND e.user_id=?
                    """,
                    (sweep_id, body.experiment_id, sweep_row["user_id"]),
                )
            ).fetchone()
            if member_row is None:
                raise HTTPException(
                    status_code=422,
                    detail="所选实验不是该参数扫描的成员",
                )
            if member_row["status"] != "completed":
                raise HTTPException(
                    status_code=409,
                    detail="只有已完成的扫描成员才能晋升",
                )
            locked_trust_profile, locked_trust_assessment = (
                await _locked_test_research_binding(
                    member=dict(member_row),
                    sweep=dict(sweep_row),
                )
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"晋升锁定测试预检失败: {exc}")

    try:
        async for conn in get_db("experiment"):
            await conn.execute("BEGIN IMMEDIATE")
            try:
                sweep_query = "SELECT * FROM param_sweeps WHERE id = ?"
                sweep_params: list[Any] = [sweep_id]
                if not user.get("is_admin"):
                    sweep_query += " AND user_id = ?"
                    sweep_params.append(user["id"])
                cursor = await conn.execute(sweep_query, sweep_params)
                sweep = await cursor.fetchone()
                if sweep is None:
                    raise HTTPException(
                        status_code=404,
                        detail=f"参数扫描不存在或无权访问: {sweep_id}",
                    )
                if not sweep["locked_test_start"] or not sweep["locked_test_end"]:
                    raise HTTPException(
                        status_code=422,
                        detail="旧版未锁定扫描不能晋升到锁定测试",
                    )

                if sweep["promoted_experiment_id"] is not None:
                    if (
                        sweep["promotion_source_experiment_id"]
                        != body.experiment_id
                    ):
                        raise HTTPException(
                            status_code=409,
                            detail="该扫描已从另一个成员创建锁定测试实验",
                        )
                    promotion_id = int(sweep["promoted_experiment_id"])
                    await conn.commit()
                    return {
                        "data": {
                            "sweep_id": sweep_id,
                            "source_experiment_id": body.experiment_id,
                            "experiment_id": promotion_id,
                            "created": False,
                            "research_trust": "locked_test",
                        }
                    }

                cursor = await conn.execute(
                    """
                    SELECT e.*
                    FROM sweep_experiments se
                    JOIN experiments e ON e.id = se.experiment_id
                    WHERE se.sweep_id = ? AND se.experiment_id = ?
                      AND e.user_id = ?
                    """,
                    (sweep_id, body.experiment_id, sweep["user_id"]),
                )
                member = await cursor.fetchone()
                if member is None:
                    raise HTTPException(
                        status_code=422,
                        detail="所选实验不是该参数扫描的成员",
                    )
                if member["status"] != "completed":
                    raise HTTPException(
                        status_code=409,
                        detail="只有已完成的扫描成员才能晋升",
                    )

                strategy_id = member["strategy_id"]
                params = _json.loads(member["params"] or "{}")
                data_access_policy = _data_access_policy_from_run_spec(
                    member["run_spec"]
                )
                display_name = (
                    f"{sweep['name'] or strategy_id}-locked-test-"
                    f"{body.experiment_id}"
                )
                run_spec = _json.dumps(
                    {
                        "strategy_id": strategy_id,
                        "pool_preset": member["pool_preset"],
                        "pool_custom_codes": member["pool_custom_codes"],
                        "pool_industries": member["pool_industries"],
                        "train_start": member["train_start"],
                        "train_end": member["train_end"],
                        "test_start": sweep["locked_test_start"],
                        "test_end": sweep["locked_test_end"],
                        "params": params,
                        "mode": member["mode"],
                        "data_access_policy": data_access_policy,
                        "research_trust_profile": locked_trust_profile,
                        "source_experiment_id": body.experiment_id,
                        "research_trust": (
                            locked_trust_assessment
                            if locked_trust_assessment is not None
                            else "locked_test"
                        ),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                cursor = await conn.execute(
                    """
                    INSERT INTO experiments
                        (user_id, name, strategy_id, strategy_category,
                         pool_preset, pool_custom_codes, pool_industries,
                         train_start, train_end, test_start, test_end,
                         params, params_hash, mode, requires_training,
                         retrain_frequency, status, progress_pct,
                         progress_message, run_spec, source_experiment_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            'pending', 0, '等待锁定测试执行', ?, ?)
                    """,
                    (
                        sweep["user_id"],
                        display_name,
                        strategy_id,
                        member["strategy_category"],
                        member["pool_preset"],
                        member["pool_custom_codes"],
                        member["pool_industries"],
                        member["train_start"],
                        member["train_end"],
                        sweep["locked_test_start"],
                        sweep["locked_test_end"],
                        member["params"],
                        member["params_hash"],
                        member["mode"],
                        member["requires_training"],
                        member["retrain_frequency"],
                        run_spec,
                        body.experiment_id,
                    ),
                )
                promotion_id = int(cursor.lastrowid)
                await conn.execute(
                    """
                    UPDATE param_sweeps
                    SET promoted_experiment_id = ?,
                        promotion_source_experiment_id = ?,
                        promoted_at = datetime('now')
                    WHERE id = ?
                    """,
                    (promotion_id, body.experiment_id, sweep_id),
                )
                await conn.commit()
                created = True
            except Exception:
                await conn.rollback()
                raise
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"晋升锁定测试失败: {exc}")

    if promotion_id is None:
        raise HTTPException(status_code=500, detail="晋升锁定测试失败: 未创建实验")

    broker = get_job_broker()
    try:
        job_id = await broker.submit_job(
            job_type="backtest",
            params={
                "experiment_id": promotion_id,
                "strategy_id": strategy_id,
                "params": params,
                "test_protocol": "locked_test",
                "sweep_id": sweep_id,
                "source_experiment_id": body.experiment_id,
                "pool_preset": member["pool_preset"],
                "pool_custom_codes": member["pool_custom_codes"],
                "data_access_policy": data_access_policy,
            },
            user_id=user["id"],
            display_name=display_name,
            resource_type="experiment",
            resource_id=promotion_id,
        )
    except Exception as exc:
        async for conn in get_db("experiment"):
            await conn.execute(
                """
                UPDATE experiments
                SET status='failed', progress_pct=100,
                    progress_message='任务提交失败', error_log=?,
                    completed_at=datetime('now')
                WHERE id=?
                """,
                (f"任务提交失败: {type(exc).__name__}: {exc}", promotion_id),
            )
            await conn.commit()
        raise HTTPException(status_code=503, detail="锁定测试任务提交失败")

    return {
        "data": {
            "sweep_id": sweep_id,
            "source_experiment_id": body.experiment_id,
            "experiment_id": promotion_id,
            "job_id": str(job_id),
            "created": created,
            "research_trust": "locked_test",
        }
    }


# ═══════════════════════════════════════════════════════════════════════════
# POST /sweep — 创建参数扫描
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/sweep")
async def create_param_sweep(
    body: SweepBody,
    user: dict[str, Any] = Depends(require_permission("experiments:sweep")),
) -> dict[str, Any]:
    """创建参数扫描：生成所有参数组合并批量创建子实验。"""
    from backend.dependencies import get_strategy_registry

    registry = get_strategy_registry()
    if body.research_trust_profile == "tushare_research_trusted" and (
        body.pool_industries
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "tushare_research_trust_scope_forbidden",
                "message": "Tushare 条件信任扫描暂不支持未经认证的历史行业筛选",
            },
        )
    try:
        strategy_meta = registry.get_metadata(body.strategy_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"策略不存在: {body.strategy_id}")
    if not body.param_grid or any(not values for values in body.param_grid.values()):
        raise HTTPException(status_code=422, detail="param_grid 不能为空")
    if len(body.param_grid) > MAX_SWEEP_PARAMETERS:
        raise HTTPException(
            status_code=422,
            detail=f"单次参数扫描最多包含 {MAX_SWEEP_PARAMETERS} 个参数",
        )
    oversized = [
        name
        for name, values in body.param_grid.items()
        if len(values) > MAX_SWEEP_VALUES_PER_PARAMETER
    ]
    if oversized:
        raise HTTPException(
            status_code=422,
            detail=(
                f"每个参数最多包含 {MAX_SWEEP_VALUES_PER_PARAMETER} 个候选值: "
                + ", ".join(sorted(oversized))
            ),
        )
    supported_modes = {
        item.value if hasattr(item, "value") else str(item)
        for item in strategy_meta.supported_modes
    }
    if body.mode not in supported_modes:
        raise HTTPException(
            status_code=422,
            detail=f"策略不支持运行模式 {body.mode}",
        )
    if _require_training_window(strategy_meta) and not (
        body.train_start and body.train_end
    ):
        raise HTTPException(
            status_code=422,
            detail="一次训练模型的参数扫描必须提供完整训练窗口",
        )

    # 生成所有参数组合
    param_names = list(body.param_grid.keys())
    param_values = list(body.param_grid.values())
    combination_count = math.prod(len(values) for values in param_values)
    if combination_count > MAX_SWEEP_EXPERIMENTS:
        raise HTTPException(
            status_code=422,
            detail=f"单次参数扫描最多生成 {MAX_SWEEP_EXPERIMENTS} 个实验",
        )
    combinations = list(itertools.product(*param_values))
    for combo in combinations:
        candidate = {**body.base_params, **dict(zip(param_names, combo))}
        is_valid, error = registry.validate_params(body.strategy_id, candidate)
        if not is_valid:
            raise HTTPException(status_code=422, detail=f"参数组合无效: {error}")

    sweep_config = _json.dumps(body.param_grid, ensure_ascii=False)
    category = strategy_meta.category.value if hasattr(strategy_meta.category, "value") else str(strategy_meta.category)
    selection_start, selection_end = body.selection_window()
    research_trust = body.research_trust()
    data_access_policy = body.data_access_policy
    research_trust_profile = body.research_trust_profile
    if body.source_experiment_id is not None:
        async for source_conn in get_db("experiment"):
            source_experiment = await _get_source_experiment(
                source_conn,
                body.source_experiment_id,
                user,
                strategy_id=body.strategy_id,
            )
        inherited_policy = _data_access_policy_from_run_spec(
            source_experiment["run_spec"]
        )
        if (
            "data_access_policy" in body.model_fields_set
            and body.data_access_policy != inherited_policy
        ):
            raise HTTPException(
                status_code=422,
                detail="参数扫描的数据访问策略必须继承基准实验",
            )
        data_access_policy = inherited_policy
        inherited_trust_profile = _research_trust_from_run_spec(
            source_experiment["run_spec"]
        )["profile"]
        if (
            "research_trust_profile" in body.model_fields_set
            and body.research_trust_profile != inherited_trust_profile
        ):
            raise HTTPException(
                status_code=422,
                detail="参数扫描的研究信任档案必须继承基准实验",
            )
        research_trust_profile = inherited_trust_profile

    # Selection members must bind only the window they will execute.  A future
    # locked test is bound separately at promotion time to the same immutable
    # generation; storing its longer actual window here makes every selection
    # worker correctly reject its own shorter load as semantic drift.
    research_trust_assessment = await _require_pit_submission(
        pool_id=body.pool_preset,
        train_start=body.train_start,
        test_start=selection_start,
        test_end=selection_end,
        data_access_policy=data_access_policy,
        research_trust_profile=research_trust_profile,
        purpose="tuning",
    )
    if research_trust_assessment is not None and body.pool_industries:
        research_trust_assessment.setdefault("warnings", []).extend(
            [
                "industry_filter_uses_current_classification",
                "historical_industry_neutralization_not_proven",
            ]
        )

    try:
        async for conn in get_db("experiment"):
            # 创建 sweep 记录
            cursor = await conn.execute(
                """
                INSERT INTO param_sweeps
                    (user_id, strategy_id, name, sweep_config,
                     selection_start, selection_end,
                     locked_test_start, locked_test_end, research_trust,
                     total_experiments, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                """,
                (
                    user["id"],
                    body.strategy_id,
                    body.name or f"Sweep-{body.strategy_id}",
                    sweep_config,
                    selection_start,
                    selection_end,
                    body.locked_test_start,
                    body.locked_test_end,
                    research_trust,
                    len(combinations),
                ),
            )
            sweep_id = cursor.lastrowid

            # 批量创建子实验
            experiment_ids: list[int] = []
            for combo in combinations:
                combo_params = {**body.base_params, **dict(zip(param_names, combo))}
                params_str = _json.dumps(combo_params, ensure_ascii=False)
                params_hash = _hash_params(combo_params)
                run_spec = _json.dumps(
                    {
                        "strategy_id": body.strategy_id,
                        "pool_preset": body.pool_preset,
                        "pool_custom_codes": body.pool_custom_codes,
                        "pool_industries": body.pool_industries,
                        "train_start": body.train_start,
                        "train_end": body.train_end,
                        "test_start": selection_start,
                        "test_end": selection_end,
                        "params": combo_params,
                        "mode": body.mode,
                        "data_access_policy": data_access_policy,
                        "research_trust_profile": research_trust_profile,
                        "research_trust": research_trust_assessment,
                        "source_experiment_id": body.source_experiment_id,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )

                cursor = await conn.execute(
                    """
                    INSERT INTO experiments
                        (user_id, name, strategy_id, strategy_category,
                         pool_preset, pool_custom_codes, pool_industries,
                         train_start, train_end, test_start, test_end,
                         params, params_hash, mode, requires_training,
                         retrain_frequency, status, progress_pct,
                         progress_message, run_spec, source_experiment_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, '等待扫描执行', ?, ?)
                    """,
                    (
                        user["id"],
                        f"{body.name or 'Sweep'}-{len(experiment_ids)+1}",
                        body.strategy_id,
                        category,
                        body.pool_preset,
                        body.pool_custom_codes,
                        body.pool_industries,
                        body.train_start,
                        body.train_end,
                        selection_start,
                        selection_end,
                        params_str,
                        params_hash,
                        body.mode,
                        1 if strategy_meta.requires_training else 0,
                        strategy_meta.retrain_frequency.value
                        if hasattr(strategy_meta.retrain_frequency, "value")
                        else str(strategy_meta.retrain_frequency),
                        run_spec,
                        body.source_experiment_id,
                    ),
                )
                exp_id = cursor.lastrowid
                experiment_ids.append(exp_id)

                # 关联 sweep 和 experiment
                await conn.execute(
                    "INSERT INTO sweep_experiments (sweep_id, experiment_id, param_combo) VALUES (?, ?, ?)",
                    (sweep_id, exp_id, _json.dumps(combo_params, ensure_ascii=False)),
                )

            await conn.commit()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"创建参数扫描失败: {e}")

    # 原子提交整组任务；调度器仅在全部成员入队后被唤醒。
    broker = get_job_broker()
    jobs: list[str] = []
    submission_failures: list[tuple[int, str]] = []
    submissions = [
        {
            "job_type": "backtest",
            "params": {
                "experiment_id": exp_id,
                "sweep_id": sweep_id,
                "pool_preset": body.pool_preset,
                "pool_custom_codes": body.pool_custom_codes,
            },
            "user_id": user["id"],
            "display_name": f"{body.name or body.strategy_id} · 实验 #{exp_id}",
            "resource_type": "experiment",
            "resource_id": exp_id,
        }
        for exp_id in experiment_ids
    ]
    batch_managed_sweep = hasattr(broker, "submit_jobs_batch")
    if batch_managed_sweep:
        try:
            jobs = await broker.submit_jobs_batch(
                submissions,
                sweep_id=sweep_id,
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            submission_failures = [(exp_id, error) for exp_id in experiment_ids]
    else:
        # Compatibility path for minimal brokers used by isolated integrations.
        for submission in submissions:
            exp_id = int(submission["params"]["experiment_id"])
            try:
                job_id = await broker.submit_job(**submission)
                jobs.append(str(job_id))
            except Exception as exc:
                submission_failures.append(
                    (exp_id, f"{type(exc).__name__}: {exc}")
                )

    if submission_failures or not batch_managed_sweep:
        async for conn in get_db("experiment"):
            if submission_failures:
                await conn.executemany(
                    """
                    UPDATE experiments
                    SET status='failed', progress_pct=100,
                        progress_message='任务提交失败', error_log=?,
                        completed_at=datetime('now')
                    WHERE id=?
                    """,
                    [
                        (f"任务提交失败: {error}", exp_id)
                        for exp_id, error in submission_failures
                    ],
                )
            await conn.execute(
                """
                UPDATE param_sweeps
                SET status=?, completed_experiments=?
                WHERE id=?
                """,
                (
                    "running" if jobs else "failed",
                    len(submission_failures),
                    sweep_id,
                ),
            )
            await conn.commit()

    return {
        "data": {
            "sweep_id": sweep_id,
            "total_experiments": len(combinations),
            "experiment_ids": experiment_ids,
            "job_ids": jobs,
            "failed_experiment_ids": [
                exp_id for exp_id, _ in submission_failures
            ],
            "selection_window": {
                "start": selection_start,
                "end": selection_end,
            },
            "locked_test_window": (
                {
                    "start": body.locked_test_start,
                    "end": body.locked_test_end,
                }
                if body.locked_test_start and body.locked_test_end
                else None
            ),
            "research_trust": research_trust,
            "data_access_policy": data_access_policy,
        }
    }


# MOVED: GET /sweep/{sweep_id} 已移到 GET /{experiment_id} 之前定义


# ═══════════════════════════════════════════════════════════════════════════
# POST /compare — 多实验对比
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/compare")
async def compare_experiments(
    body: CompareBody,
    user: dict[str, Any] = Depends(require_permission("experiments:read")),
) -> dict[str, Any]:
    """多实验指标对比。"""
    if len(body.experiment_ids) < 2:
        raise HTTPException(status_code=400, detail="至少需要2个实验进行对比")
    if len(body.experiment_ids) > 10:
        raise HTTPException(status_code=400, detail="最多支持10个实验对比")

    try:
        async for conn in get_db("experiment"):
            placeholders = ",".join("?" * len(body.experiment_ids))
            # FIXED: reviewer issue #11 — 增加 user_id 过滤
            if not user.get("is_admin"):
                query = f"""
                    SELECT e.id, e.name, e.strategy_id, e.params, e.test_start, e.test_end, e.created_at,
                           m.*
                    FROM experiments e
                    LEFT JOIN experiment_metrics m ON e.id = m.experiment_id
                    WHERE e.id IN ({placeholders}) AND e.user_id = ?
                    ORDER BY m.sharpe_ratio DESC
                """
                cursor = await conn.execute(query, body.experiment_ids + [user["id"]])
            else:
                query = f"""
                    SELECT e.id, e.name, e.strategy_id, e.params, e.test_start, e.test_end, e.created_at,
                           m.*
                    FROM experiments e
                    LEFT JOIN experiment_metrics m ON e.id = m.experiment_id
                    WHERE e.id IN ({placeholders})
                    ORDER BY m.sharpe_ratio DESC
                """
                cursor = await conn.execute(query, body.experiment_ids)
            rows = await cursor.fetchall()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"对比查询失败: {e}")

    items: list[dict[str, Any]] = []
    for row in rows:
        d = _row_to_dict(row)
        d["params"] = _json.loads(d["params"]) if d.get("params") else {}
        items.append(d)

    return {"data": items}
