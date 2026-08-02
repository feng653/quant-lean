"""策略 API — 策略注册中心的管理和查询接口."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.api.timestamps import serialize_utc_timestamp
from backend.dependencies import (
    get_db,
    get_strategy_registry,
    require_permission,
)
from backend.services.experiment_eligibility import assess_experiment_eligibility
from backend.strategies.base import (
    EXECUTION_CONFIG_PARAM,
    PlatformExecutionConfig,
    StrategyCategory,
)

router = APIRouter(prefix="/api/strategies", tags=["Strategies"])


def _training_mode(meta: Any) -> str:
    if not meta.requires_training:
        return "none"
    frequency = (
        meta.retrain_frequency.value
        if hasattr(meta.retrain_frequency, "value")
        else str(meta.retrain_frequency)
    )
    return "train_once" if frequency == "never" else "periodic"


def _execution_config_contract() -> dict[str, Any]:
    return {
        "param_key": EXECUTION_CONFIG_PARAM,
        "defaults": asdict(PlatformExecutionConfig()),
    }


# ═══════════════════════════════════════════════════════════════════════════
# GET / — 列出所有策略
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/")
async def list_strategies(
    category: str | None = Query(None, description="按分类筛选: technical|ml|factor|portfolio|composite"),
    user: dict[str, Any] = Depends(require_permission("strategies:read")),
) -> dict[str, Any]:
    """列出所有已注册策略，支持按分类筛选。"""
    registry = get_strategy_registry()

    try:
        if category:
            # FIXED: reviewer issue #2 — 字符串转 StrategyCategory 枚举
            try:
                cat_enum = StrategyCategory(category)
            except ValueError:
                raise HTTPException(status_code=400, detail=f"无效的策略分类: {category}，有效值: {[c.value for c in StrategyCategory]}")
            strategies = registry.list_by_category(cat_enum)
        else:
            # FIXED: reviewer issue #2 — 使用 list_all() 替代 get_all_strategies()
            strategies = registry.list_all()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取策略列表失败: {e}")

    items: list[dict[str, Any]] = []
    for meta in strategies:
        items.append({
            "strategy_id": meta.strategy_id,
            "display_name": meta.display_name,
            "version": meta.version,
            "category": meta.category.value if hasattr(meta.category, "value") else str(meta.category),
            "description": meta.description,
            "supported_modes": [m.value if hasattr(m, "value") else str(m) for m in meta.supported_modes],
            "requires_training": meta.requires_training,
            "retrain_frequency": meta.retrain_frequency.value if hasattr(meta.retrain_frequency, "value") else str(meta.retrain_frequency),
            "training_mode": _training_mode(meta),
            "portfolio_signal_mode": (
                meta.portfolio_signal_mode.value
                if hasattr(meta.portfolio_signal_mode, "value")
                else str(meta.portfolio_signal_mode)
            ),
            "execution_config": _execution_config_contract(),
            "params": [
                {
                    "name": p.name,
                    "type": p.type,
                    "default": p.default,
                    "description": p.description,
                    "required": p.required,
                    "min": p.min,
                    "max": p.max,
                    "step": p.step,
                    "choices": p.choices,
                }
                for p in meta.params
            ],
            "sub_strategies": [
                {"strategy_id": s.strategy_id, "role": s.role}
                for s in meta.sub_strategies
            ],
            "tags": meta.tags,
        })

    return {"data": items}


# ═══════════════════════════════════════════════════════════════════════════
# POST /scan — 扫描目录热加载
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/scan")
async def scan_strategies(
    user: dict[str, Any] = Depends(require_permission("strategies:scan")),
) -> dict[str, Any]:
    """扫描策略目录，热加载新策略。"""
    from backend.config import settings

    registry = get_strategy_registry()

    try:
        strategies_dir = settings.PROJECT_ROOT / "backend" / "strategies"
        # FIXED: reviewer issue #2 — 使用 list_all() 替代 get_all_strategies()
        count_before = len(registry.list_all())
        registry.scan_directory(strategies_dir)
        count_after = len(registry.list_all())
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"策略扫描失败: {e}")

    return {
        "data": {
            "before": count_before,
            "after": count_after,
            "added": count_after - count_before,
        }
    }


# ═══════════════════════════════════════════════════════════════════════════
# GET /{strategy_id} — 策略详情
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/{strategy_id}")
async def get_strategy_detail(
    strategy_id: str,
    user: dict[str, Any] = Depends(require_permission("strategies:read")),
) -> dict[str, Any]:
    """获取策略详情，含实验数/部署数统计。"""
    registry = get_strategy_registry()

    try:
        # FIXED: reviewer issue #2 — get_strategy() 返回实例，改用 get_metadata() 获取元数据
        meta = registry.get_metadata(strategy_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"策略不存在: {strategy_id}")

    # 统计实验数和部署数
    experiment_count = 0
    deployment_count = 0

    try:
        async for conn in get_db("experiment"):
            cursor = await conn.execute(
                "SELECT COUNT(*) as cnt FROM experiments WHERE strategy_id = ?",
                (strategy_id,),
            )
            row = await cursor.fetchone()
            if row:
                experiment_count = row["cnt"]
    except Exception:
        pass

    try:
        async for conn in get_db("trading_sim"):
            cursor = await conn.execute(
                "SELECT COUNT(*) as cnt FROM deployments WHERE strategy_id = ?",
                (strategy_id,),
            )
            row = await cursor.fetchone()
            if row:
                deployment_count = row["cnt"]
    except Exception:
        pass

    return {
        "data": {
            "strategy_id": meta.strategy_id,
            "display_name": meta.display_name,
            "version": meta.version,
            "category": meta.category.value if hasattr(meta.category, "value") else str(meta.category),
            "description": meta.description,
            "supported_modes": [m.value if hasattr(m, "value") else str(m) for m in meta.supported_modes],
            "requires_training": meta.requires_training,
            "retrain_frequency": meta.retrain_frequency.value if hasattr(meta.retrain_frequency, "value") else str(meta.retrain_frequency),
            "training_mode": _training_mode(meta),
            "portfolio_signal_mode": (
                meta.portfolio_signal_mode.value
                if hasattr(meta.portfolio_signal_mode, "value")
                else str(meta.portfolio_signal_mode)
            ),
            "execution_config": _execution_config_contract(),
            "estimated_training_seconds": meta.estimated_training_seconds,
            "max_position_pct": meta.max_position_pct,
            "supported_position_modes": meta.supported_position_modes,
            "params": [
                {
                    "name": p.name,
                    "type": p.type,
                    "default": p.default,
                    "description": p.description,
                    "required": p.required,
                    "min": p.min,
                    "max": p.max,
                    "step": p.step,
                    "choices": p.choices,
                }
                for p in meta.params
            ],
            "sub_strategies": [
                {"strategy_id": s.strategy_id, "role": s.role, "params_override": s.params_override}
                for s in meta.sub_strategies
            ],
            "integration_method": meta.integration_method,
            "tags": meta.tags,
            "experiment_count": experiment_count,
            "deployment_count": deployment_count,
        }
    }


# ═══════════════════════════════════════════════════════════════════════════
# GET /{strategy_id}/sub-strategies — 子策略
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/{strategy_id}/sub-strategies")
async def get_sub_strategies(
    strategy_id: str,
    user: dict[str, Any] = Depends(require_permission("strategies:read")),
) -> dict[str, Any]:
    """获取组合策略的子策略列表。"""
    registry = get_strategy_registry()

    try:
        registry.get_metadata(strategy_id)
        sub_refs = registry.get_sub_strategies(strategy_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"策略不存在: {strategy_id}")

    items: list[dict[str, Any]] = []
    for ref in sub_refs:
        try:
            sub_meta = registry.get_metadata(ref.strategy_id)
            items.append({
                "strategy_id": ref.strategy_id,
                "display_name": sub_meta.display_name,
                "category": sub_meta.category.value if hasattr(sub_meta.category, "value") else str(sub_meta.category),
                "role": ref.role,
                "params_override": ref.params_override,
            })
        except KeyError:
            items.append({
                "strategy_id": ref.strategy_id,
                "display_name": "未知策略",
                "role": ref.role,
                "params_override": ref.params_override,
            })

    return {"data": items}


# ═══════════════════════════════════════════════════════════════════════════
# GET /{strategy_id}/parent-strategies — 被哪些组合引用
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/{strategy_id}/parent-strategies")
async def get_parent_strategies(
    strategy_id: str,
    user: dict[str, Any] = Depends(require_permission("strategies:read")),
) -> dict[str, Any]:
    """获取引用了该策略的所有组合策略。"""
    registry = get_strategy_registry()

    try:
        parent_ids = registry.get_parent_strategies(strategy_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询失败: {e}")

    items: list[dict[str, Any]] = []
    for pid in parent_ids:
        try:
            meta = registry.get_metadata(pid)
            items.append({
                "strategy_id": meta.strategy_id,
                "display_name": meta.display_name,
                "category": meta.category.value if hasattr(meta.category, "value") else str(meta.category),
            })
        except KeyError:
            items.append({"strategy_id": pid, "display_name": "未知策略"})

    return {"data": items}


# ═══════════════════════════════════════════════════════════════════════════
# GET /{strategy_id}/best-experiments — 最佳实验
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/{strategy_id}/best-experiments")
async def get_best_experiments(
    strategy_id: str,
    limit: int = Query(10, ge=1, le=100, description="返回数量上限"),
    user: dict[str, Any] = Depends(require_permission("strategies:read")),
) -> dict[str, Any]:
    """获取该策略的标注实验（starred/labeled），按 Sharpe 排序。"""
    try:
        async for conn in get_db("experiment"):
            cursor = await conn.execute(
                """
                SELECT
                    e.id, e.name, e.is_starred, e.labels,
                    e.train_start, e.train_end, e.test_start, e.test_end,
                    e.params, e.status, e.created_at,
                    m.sharpe_ratio, m.annual_return, m.max_drawdown,
                    rm.schema_version AS manifest_schema_version,
                    rm.manifest_json, rm.manifest_hash
                FROM experiments e
                LEFT JOIN experiment_metrics m ON e.id = m.experiment_id
                JOIN research_run_manifests rm ON rm.experiment_id = e.id
                WHERE e.strategy_id = ?
                  AND e.status = 'completed'
                  AND (e.is_starred = 1 OR e.labels IS NOT NULL)
                ORDER BY m.sharpe_ratio DESC
                LIMIT ?
                """,
                (strategy_id, min(max(limit * 20, limit), 1000)),
            )
            rows = await cursor.fetchall()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询失败: {e}")

    import json as _json

    items: list[dict[str, Any]] = []
    for row in rows:
        eligibility = assess_experiment_eligibility(
            experiment_id=int(row["id"]),
            strategy_id=strategy_id,
            manifest_json=row["manifest_json"],
            manifest_hash=row["manifest_hash"],
            schema_version=row["manifest_schema_version"],
        )
        if not eligibility.eligible:
            continue
        items.append({
            "id": row["id"],
            "name": row["name"],
            "is_starred": bool(row["is_starred"]),
            "labels": _json.loads(row["labels"]) if row["labels"] else [],
            "train_start": row["train_start"],
            "train_end": row["train_end"],
            "test_start": row["test_start"],
            "test_end": row["test_end"],
            "params": _json.loads(row["params"]) if row["params"] else {},
            "status": row["status"],
            "created_at": serialize_utc_timestamp(row["created_at"]),
            "sharpe_ratio": row["sharpe_ratio"],
            "annual_return": row["annual_return"],
            "max_drawdown": row["max_drawdown"],
            **eligibility.public_dict(),
        })
        if len(items) >= limit:
            break

    return {"data": items}


# ═══════════════════════════════════════════════════════════════════════════
# POST /{strategy_id}/validate — 校验参数
# ═══════════════════════════════════════════════════════════════════════════

from pydantic import BaseModel


class ValidateParamsBody(BaseModel):
    params: dict[str, Any]


@router.post("/{strategy_id}/validate")
async def validate_strategy_params(
    strategy_id: str,
    body: ValidateParamsBody,
    user: dict[str, Any] = Depends(require_permission("strategies:read")),
) -> dict[str, Any]:
    """校验策略参数是否合法。"""
    registry = get_strategy_registry()

    try:
        registry.get_strategy(strategy_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"策略不存在: {strategy_id}")

    try:
        valid, message = registry.validate_params(strategy_id, body.params)
    except Exception as e:
        valid, message = False, str(e)

    return {"data": {"valid": valid, "message": message}}


def _validate_params_from_metadata(meta: Any, params: dict[str, Any]) -> tuple[bool, str]:
    """基于元数据做基本参数校验。"""
    for p in meta.params:
        if p.required and p.name not in params:
            return False, f"缺少必填参数: {p.name}"
        if p.name in params:
            val = params[p.name]
            if p.type == "int" and not isinstance(val, int):
                return False, f"参数 {p.name} 应为整数"
            if p.type == "float" and not isinstance(val, (int, float)):
                return False, f"参数 {p.name} 应为浮点数"
            if p.type == "bool" and not isinstance(val, bool):
                return False, f"参数 {p.name} 应为布尔值"
            if p.type == "choice" and p.choices and val not in p.choices:
                return False, f"参数 {p.name} 值 {val} 不在可选范围 {p.choices}"
            if p.min is not None and val < p.min:
                return False, f"参数 {p.name} 值 {val} 小于最小值 {p.min}"
            if p.max is not None and val > p.max:
                return False, f"参数 {p.name} 值 {val} 大于最大值 {p.max}"

    return True, "参数校验通过"
