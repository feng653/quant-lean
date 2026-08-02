"""Governed AI API for analysis, suggestions, diagnosis, and explanations."""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.ai.service import AiInvocationResult, AiService
from backend.data.cache import DataCache
from backend.data.sources.akshare_source import AKShareSource
from backend.data.universe import UniverseManager
from backend.dependencies import get_db, get_strategy_registry, require_permission

router = APIRouter(prefix="/api/ai", tags=["AI"])

DIAGNOSIS_CATEGORIES = {
    "strategy_interface",
    "strategy_code",
    "data",
    "params",
    "environment",
    "unknown",
}


class AnalyzeBacktestBody(BaseModel):
    experiment_id: int


class SuggestParamsBody(BaseModel):
    strategy_id: str
    current_params: dict[str, Any] = Field(default_factory=dict)


class MarketInsightBody(BaseModel):
    portfolio_id: int


class DiagnoseErrorBody(BaseModel):
    experiment_id: int
    error_log: str


class ExplainSignalBody(BaseModel):
    strategy_id: str
    signal: dict[str, Any]
    context: dict[str, Any] | None = None


@dataclass(frozen=True)
class _TimedInvocation:
    result: AiInvocationResult
    latency_ms: float

    def __getattr__(self, name: str) -> Any:
        return getattr(self.result, name)


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key] if key in row.keys() else default
    except (AttributeError, KeyError, TypeError):
        return default


def _json_text(value: Any, *, indent: int | None = None) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=indent,
        separators=None if indent is not None else (",", ":"),
    )


def _result_metadata(result: _TimedInvocation) -> dict[str, Any]:
    return {
        "cached": result.cached,
        "model": result.model,
        "usage": {
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "total_tokens": result.total_tokens,
            "latency_ms": result.latency_ms,
        },
    }


async def _invoke(
    endpoint: str,
    user_id: int | None,
    prompt_template: str,
    *,
    cache_context: Any,
    failure_label: str,
    validator: Callable[[str], Any] | None = None,
    **kwargs: Any,
) -> _TimedInvocation:
    started = time.perf_counter()
    try:
        result = await AiService().invoke(
            endpoint,
            user_id,
            prompt_template,
            cache_context=cache_context,
            validator=validator,
            **kwargs,
        )
        return _TimedInvocation(
            result=result,
            latency_ms=round((time.perf_counter() - started) * 1000, 3),
        )
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"{failure_label}: {exc}"
        ) from exc


def _strict_json_object(text: str, label: str) -> dict[str, Any]:
    def reject_constant(value: str) -> None:
        raise ValueError(f"非法 JSON 常量: {value}")

    try:
        payload = json.loads(text, parse_constant=reject_constant)
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(
            status_code=502, detail=f"AI 返回的{label}不是合法 JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail=f"AI 返回的{label}必须是 JSON 对象")
    return payload


def _validate_field_value(field: Any, value: Any) -> bool:
    if field.type in {"int", "integer"}:
        valid_type = not isinstance(value, bool) and isinstance(value, int)
    elif field.type in {"float", "number"}:
        valid_type = not isinstance(value, bool) and isinstance(value, (int, float))
        if valid_type and not math.isfinite(float(value)):
            return False
    elif field.type in {"bool", "boolean"}:
        valid_type = isinstance(value, bool)
    elif field.type in {"str", "string", "choice"}:
        valid_type = isinstance(value, str)
    else:
        valid_type = True
    if not valid_type:
        return False
    if field.choices is not None and value not in field.choices:
        return False
    try:
        if field.min is not None and value < field.min:
            return False
        if field.max is not None and value > field.max:
            return False
    except TypeError:
        return False
    return True


def _parse_suggestions(
    text: str,
    metadata: Any,
    current_params: dict[str, Any],
) -> list[dict[str, Any]]:
    payload = _strict_json_object(text, "参数建议")
    if set(payload) != {"suggestions"} or not isinstance(
        payload["suggestions"], list
    ):
        raise HTTPException(
            status_code=502,
            detail="AI 参数建议必须只包含 suggestions 数组",
        )
    definitions = {field.name: field for field in metadata.params}
    parsed: list[dict[str, Any]] = []
    seen: set[str] = set()
    required = {"param_name", "current_value", "suggested_value", "reason"}
    for item in payload["suggestions"]:
        if not isinstance(item, dict) or set(item) != required:
            raise HTTPException(status_code=502, detail="AI 参数建议项字段不符合契约")
        name = item["param_name"]
        if not isinstance(name, str) or name not in definitions:
            raise HTTPException(status_code=502, detail=f"AI 建议了未知参数: {name}")
        if name in seen:
            raise HTTPException(status_code=502, detail=f"AI 重复建议参数: {name}")
        expected_current = current_params.get(name, definitions[name].default)
        if item["current_value"] != expected_current:
            raise HTTPException(
                status_code=502, detail=f"AI 返回的 {name} 当前值与请求不一致"
            )
        if not _validate_field_value(definitions[name], item["suggested_value"]):
            raise HTTPException(
                status_code=502, detail=f"AI 返回的 {name} 建议值类型或范围非法"
            )
        if not isinstance(item["reason"], str) or not item["reason"].strip():
            raise HTTPException(status_code=502, detail=f"AI 返回的 {name} 理由为空")
        seen.add(name)
        parsed.append(item)
    return parsed


def _parse_diagnosis(text: str) -> dict[str, Any]:
    payload = _strict_json_object(text, "诊断")
    required = {
        "category",
        "root_cause",
        "evidence",
        "fix_suggestion",
        "auto_fixable",
    }
    if set(payload) != required:
        raise HTTPException(status_code=502, detail="AI 诊断字段不符合契约")
    if payload["category"] not in DIAGNOSIS_CATEGORIES:
        raise HTTPException(status_code=502, detail="AI 诊断 category 不在白名单")
    for key in ("root_cause", "evidence", "fix_suggestion"):
        if not isinstance(payload[key], str) or not payload[key].strip():
            raise HTTPException(status_code=502, detail=f"AI 诊断 {key} 不能为空")
    if not isinstance(payload["auto_fixable"], bool):
        raise HTTPException(status_code=502, detail="AI 诊断 auto_fixable 必须为布尔值")
    if payload["auto_fixable"] and payload["category"] not in {
        "strategy_interface",
        "strategy_code",
    }:
        raise HTTPException(
            status_code=502,
            detail="只有 strategy_interface/strategy_code 可标记 auto_fixable",
        )
    return payload


async def _build_industry_exposure(
    positions: list[Any],
) -> tuple[str, list[dict[str, Any]]]:
    if not positions:
        return "当前无持仓", []
    try:
        industry_map = await UniverseManager(
            AKShareSource(), DataCache()
        ).get_industry_map()
    except Exception:
        industry_map = {}
    totals: dict[str, float] = {}
    for position in positions:
        code = str(position["code"])
        try:
            market_value = max(float(position["market_value"] or 0), 0.0)
        except (TypeError, ValueError):
            market_value = 0.0
        industry = industry_map.get(code, "未知")
        totals[industry] = totals.get(industry, 0.0) + market_value
    portfolio_value = sum(totals.values())
    summary = [
        {
            "industry": industry,
            "market_value": round(market_value, 2),
            "weight_pct": (
                round(market_value / portfolio_value * 100, 4)
                if portfolio_value > 0
                else 0.0
            ),
        }
        for industry, market_value in sorted(
            totals.items(), key=lambda item: (-item[1], item[0])
        )
    ]
    return _json_text(summary, indent=2), summary


@router.post("/analyze-backtest")
async def analyze_backtest(
    body: AnalyzeBacktestBody,
    user: dict[str, Any] = Depends(require_permission("ai:use")),
) -> dict[str, Any]:
    try:
        async for conn in get_db("experiment"):
            cursor = await conn.execute(
                """
                SELECT e.*, m.*
                FROM experiments e
                LEFT JOIN experiment_metrics m ON e.id = m.experiment_id
                WHERE e.id = ? AND e.status = 'completed'
                  AND (? = 1 OR e.user_id = ?)
                """,
                (body.experiment_id, int(bool(user.get("is_admin"))), user["id"]),
            )
            row = await cursor.fetchone()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"查询实验失败: {exc}") from exc
    if row is None:
        raise HTTPException(
            status_code=404, detail=f"实验不存在或未完成: {body.experiment_id}"
        )

    registry = get_strategy_registry()
    strategy_name = row["strategy_id"]
    try:
        strategy_name = registry.get_metadata(row["strategy_id"]).display_name
    except (KeyError, AttributeError):
        pass
    metrics_keys = [
        ("sharpe_ratio", "Sharpe比率"),
        ("annual_return", "年化收益率"),
        ("max_drawdown", "最大回撤"),
        ("volatility", "年化波动率"),
        ("calmar_ratio", "Calmar比率"),
        ("sortino_ratio", "Sortino比率"),
        ("win_rate", "胜率"),
        ("profit_loss_ratio", "盈亏比"),
        ("total_trades", "总交易次数"),
        ("alpha", "Alpha"),
        ("beta", "Beta"),
        ("information_ratio", "信息比率"),
    ]
    lines = []
    for key, label in metrics_keys:
        value = _row_value(row, key)
        if value is not None:
            lines.append(
                f"- {label}: {value:.4f}"
                if isinstance(value, float)
                else f"- {label}: {value}"
            )
    from backend.ai.prompts import BACKTEST_ANALYSIS_PROMPT

    result = await _invoke(
        "analyze-backtest",
        user.get("id"),
        BACKTEST_ANALYSIS_PROMPT,
        cache_context={
            "experiment_id": body.experiment_id,
            "completed_at": _row_value(row, "completed_at"),
            "data_version": _row_value(row, "data_version"),
        },
        failure_label="AI 分析失败",
        strategy_name=strategy_name,
        experiment_id=body.experiment_id,
        train_start=_row_value(row, "train_start") or "N/A",
        test_end=_row_value(row, "test_end") or "N/A",
        metrics_summary="\n".join(lines) if lines else "暂无指标数据",
    )
    return {
        "data": {
            "experiment_id": body.experiment_id,
            "analysis": result.text,
            **_result_metadata(result),
        }
    }


@router.post("/suggest-params")
async def suggest_params(
    body: SuggestParamsBody,
    user: dict[str, Any] = Depends(require_permission("ai:use")),
) -> dict[str, Any]:
    registry = get_strategy_registry()
    try:
        metadata = registry.get_metadata(body.strategy_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail=f"策略不存在: {body.strategy_id}"
        ) from exc

    param_summary = [
        {
            "name": field.name,
            "type": field.type,
            "default": field.default,
            "min": field.min,
            "max": field.max,
            "choices": field.choices,
            "description": field.description,
        }
        for field in metadata.params
    ]
    history: list[dict[str, Any]] = []
    try:
        async for conn in get_db("experiment"):
            cursor = await conn.execute(
                """
                SELECT e.id, e.params, m.sharpe_ratio, m.annual_return,
                       m.max_drawdown, m.win_rate
                FROM experiments e
                LEFT JOIN experiment_metrics m ON e.id = m.experiment_id
                WHERE e.strategy_id = ? AND e.status = 'completed'
                  AND (? = 1 OR e.user_id = ?)
                ORDER BY m.sharpe_ratio DESC, e.id ASC
                LIMIT 3
                """,
                (body.strategy_id, int(bool(user.get("is_admin"))), user["id"]),
            )
            rows = await cursor.fetchall()
            for row in rows:
                try:
                    params = json.loads(row["params"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    params = {}
                history.append(
                    {
                        "experiment_id": row["id"],
                        "params": params,
                        "sharpe_ratio": row["sharpe_ratio"],
                        "annual_return": row["annual_return"],
                        "max_drawdown": row["max_drawdown"],
                        "win_rate": row["win_rate"],
                    }
                )
    except Exception:
        history = []

    category = (
        metadata.category.value
        if hasattr(metadata.category, "value")
        else str(metadata.category)
    )
    from backend.ai.prompts import PARAM_SUGGESTION_PROMPT

    cache_context = {
        "strategy": {
            "strategy_id": metadata.strategy_id,
            "version": metadata.version,
            "category": category,
            "params": param_summary,
        },
        "current_params": body.current_params,
        "historical_best": history,
    }
    result = await _invoke(
        "suggest-params",
        user.get("id"),
        PARAM_SUGGESTION_PROMPT,
        cache_context=cache_context,
        failure_label="AI 建议失败",
        validator=lambda text: _parse_suggestions(
            text,
            metadata,
            body.current_params,
        ),
        strategy_name=metadata.display_name,
        strategy_category=category,
        strategy_description=metadata.description,
        current_params=_json_text(body.current_params, indent=2),
        param_definitions=_json_text(param_summary, indent=2),
        best_metrics=_json_text(history, indent=2),
    )
    suggestions = result.structured
    return {
        "data": {
            "strategy_id": body.strategy_id,
            "suggestion": result.text,
            "suggestions": suggestions,
            **_result_metadata(result),
        }
    }


@router.post("/market-insight")
async def market_insight(
    body: MarketInsightBody,
    user: dict[str, Any] = Depends(require_permission("ai:use")),
) -> dict[str, Any]:
    try:
        async for conn in get_db("trading_sim"):
            cursor = await conn.execute(
                """
                SELECT * FROM portfolios
                WHERE id = ? AND (? = 1 OR user_id = ?)
                """,
                (body.portfolio_id, int(bool(user.get("is_admin"))), user["id"]),
            )
            portfolio = await cursor.fetchone()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"查询组合失败: {exc}") from exc
    if portfolio is None:
        raise HTTPException(status_code=404, detail=f"组合不存在: {body.portfolio_id}")

    try:
        allocations = json.loads(portfolio["allocations"] or "[]")
    except (json.JSONDecodeError, TypeError):
        allocations = []
    positions: list[Any] = []
    nav_rows: list[Any] = []
    try:
        async for conn in get_db("trading_sim"):
            cursor = await conn.execute(
                """
                SELECT code, market_value, date
                FROM position_snapshots
                WHERE portfolio_id = ?
                  AND date = (
                    SELECT MAX(date) FROM position_snapshots WHERE portfolio_id = ?
                  )
                """,
                (body.portfolio_id, body.portfolio_id),
            )
            positions = await cursor.fetchall()
            cursor = await conn.execute(
                """
                SELECT date, nav, daily_return FROM nav_history
                WHERE portfolio_id = ? ORDER BY date DESC LIMIT 30
                """,
                (body.portfolio_id,),
            )
            nav_rows = await cursor.fetchall()
    except Exception:
        nav_rows = []
    recent_performance = (
        "\n".join(
            f"日期={row['date']}, NAV={row['nav']:.2f}, "
            f"日收益={(row['daily_return'] or 0):.4%}"
            for row in nav_rows[:10]
        )
        if nav_rows
        else "暂无表现数据"
    )
    latest_nav_date = nav_rows[0]["date"] if nav_rows else None
    latest_position_date = positions[0]["date"] if positions else None
    industry_exposure, industry_summary = await _build_industry_exposure(positions)
    from backend.ai.prompts import MARKET_INSIGHT_PROMPT

    result = await _invoke(
        "market-insight",
        user.get("id"),
        MARKET_INSIGHT_PROMPT,
        cache_context={
            "portfolio_id": body.portfolio_id,
            "latest_nav_date": latest_nav_date,
            "latest_position_date": latest_position_date,
            "industry_exposure": industry_summary,
        },
        failure_label="AI 解读失败",
        portfolio_name=portfolio["name"],
        total_capital=portfolio["total_capital"],
        rebalance_frequency=portfolio["rebalance_frequency"],
        position_count=len({str(position["code"]) for position in positions}),
        allocations=_json_text(allocations, indent=2),
        recent_performance=recent_performance,
        industry_exposure=industry_exposure,
    )
    return {
        "data": {
            "portfolio_id": body.portfolio_id,
            "insight": result.text,
            **_result_metadata(result),
        }
    }


@router.post("/diagnose-error")
async def diagnose_error(
    body: DiagnoseErrorBody,
    user: dict[str, Any] = Depends(require_permission("ai:use")),
) -> dict[str, Any]:
    try:
        async for conn in get_db("experiment"):
            cursor = await conn.execute(
                """
                SELECT * FROM experiments
                WHERE id = ? AND (? = 1 OR user_id = ?)
                """,
                (body.experiment_id, int(bool(user.get("is_admin"))), user["id"]),
            )
            row = await cursor.fetchone()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"查询实验失败: {exc}") from exc
    if row is None:
        raise HTTPException(status_code=404, detail=f"实验不存在: {body.experiment_id}")

    registry = get_strategy_registry()
    strategy_name = row["strategy_id"]
    try:
        strategy_name = registry.get_metadata(row["strategy_id"]).display_name
    except (KeyError, AttributeError):
        pass
    try:
        params = json.loads(row["params"] or "{}")
    except (json.JSONDecodeError, TypeError):
        params = {}
    from backend.ai.prompts import ERROR_DIAGNOSIS_PROMPT

    updated_marker = (
        _row_value(row, "updated_at")
        or _row_value(row, "completed_at")
        or _row_value(row, "started_at")
        or _row_value(row, "created_at")
    )
    result = await _invoke(
        "diagnose-error",
        user.get("id"),
        ERROR_DIAGNOSIS_PROMPT,
        cache_context={
            "experiment_id": body.experiment_id,
            "updated_at": updated_marker,
            "completed_at": _row_value(row, "completed_at"),
            "stored_error": _row_value(row, "error_log"),
            "reported_error": body.error_log,
        },
        failure_label="AI 诊断失败",
        validator=_parse_diagnosis,
        strategy_name=strategy_name,
        experiment_id=body.experiment_id,
        status=row["status"],
        error_log=body.error_log,
        pool_preset=_row_value(row, "pool_preset") or "默认",
        pool_custom_codes=_row_value(row, "pool_custom_codes") or "无",
        train_start=_row_value(row, "train_start") or "N/A",
        train_end=_row_value(row, "train_end") or "N/A",
        test_start=_row_value(row, "test_start") or "N/A",
        test_end=_row_value(row, "test_end") or "N/A",
        params=_json_text(params, indent=2),
    )
    structured = result.structured
    diagnosis = (
        f"[{structured['category']}] {structured['root_cause']}；"
        f"建议：{structured['fix_suggestion']}"
    )
    try:
        async for conn in get_db("experiment"):
            await conn.execute(
                "UPDATE experiments SET ai_diagnosis = ? WHERE id = ?",
                (diagnosis, body.experiment_id),
            )
            await conn.commit()
    except Exception:
        pass
    return {
        "data": {
            "experiment_id": body.experiment_id,
            "diagnosis": diagnosis,
            "structured": structured,
            **_result_metadata(result),
        }
    }


@router.post("/explain-signal")
async def explain_signal(
    body: ExplainSignalBody,
    user: dict[str, Any] = Depends(require_permission("ai:use")),
) -> dict[str, Any]:
    registry = get_strategy_registry()
    try:
        metadata = registry.get_metadata(body.strategy_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail=f"策略不存在: {body.strategy_id}"
        ) from exc
    category = (
        metadata.category.value
        if hasattr(metadata.category, "value")
        else str(metadata.category)
    )
    from backend.ai.prompts import SIGNAL_EXPLAIN_PROMPT

    result = await _invoke(
        "explain-signal",
        user.get("id"),
        SIGNAL_EXPLAIN_PROMPT,
        cache_context={
            "strategy_id": body.strategy_id,
            "signal": body.signal,
            "context": body.context or {},
        },
        failure_label="AI 信号解释失败",
        strategy_name=metadata.display_name,
        strategy_category=category,
        strategy_description=metadata.description,
        code=body.signal.get("code", "未知"),
        action=body.signal.get("action", "未知"),
        score=body.signal.get("score", 0),
        confidence=body.signal.get("confidence", 0),
        context=_json_text(body.context or {}, indent=2),
    )
    return {
        "data": {
            "strategy_id": body.strategy_id,
            "explanation": result.text,
            **_result_metadata(result),
        }
    }
