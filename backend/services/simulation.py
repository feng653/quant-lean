"""Deterministic end-of-day paper-trading execution service."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite
import pandas as pd

from backend.config import settings
from backend.core.cost_model import CostModel
from backend.data.cache import DataCache, has_price_field
from backend.data.universe import POOL_NAME_ALIASES, UniverseManager
from backend.services.allocations import canonicalize_allocations
from backend.services.deployment_promotion import verify_deployment_promotion
from backend.services.model_artifacts import (
    VerifiedModelArtifact,
    load_verified_deployment_model,
)
from backend.strategies.registry import get_registry


class SimulationRunInProgressError(RuntimeError):
    """The same user/portfolio/date run is already owned by another worker."""


class PortfolioSimulationScopeError(ValueError):
    """The requested paper portfolio does not belong to the caller."""


class PortfolioSimulationBindingError(ValueError):
    """An owned portfolio has no trustworthy active deployment binding."""


def _claim_expiry() -> str:
    seconds = max(int(settings.SIMULATION_RUN_LEASE_SECONDS), 60)
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _claim_is_expired(row: Any) -> bool:
    raw = row["claim_expires_at"] if "claim_expires_at" in row.keys() else None
    if not raw:
        created = row["created_at"] if "created_at" in row.keys() else None
        if not created:
            return False
        try:
            fallback = datetime.fromisoformat(str(created)).replace(
                tzinfo=timezone.utc
            ) + timedelta(seconds=max(int(settings.SIMULATION_RUN_LEASE_SECONDS), 60))
        except ValueError:
            return False
        return fallback <= datetime.now(timezone.utc)
    try:
        expires = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return False
    return expires.replace(tzinfo=expires.tzinfo or timezone.utc) <= datetime.now(
        timezone.utc
    )


async def _simulation_side_effect_count(
    connection: aiosqlite.Connection, run_id: str
) -> int:
    count = 0
    for table in (
        "daily_signals",
        "orders",
        "position_snapshots",
        "nav_history",
        "strategy_nav_history",
    ):
        cursor = await connection.execute(
            f"SELECT COUNT(*) AS count FROM {table} "  # noqa: S608
            "WHERE simulation_run_id=?",
            (run_id,),
        )
        count += int((await cursor.fetchone())["count"])
    return count


_LOOKBACK_MARKERS = ("lookback", "period", "window", "seq_len")
_NON_SIGNAL_WINDOWS = (
    "train_months",
    "validation_months",
    "retrain_months",
    "embargo_days",
)


def derive_simulation_lookback(
    strategy_id: str,
    raw_params: Any,
) -> tuple[int, tuple[str, ...]]:
    """Derive signal warm-up from registered/default strategy parameters.

    Unknown strategies use a conservative one-trading-year window and emit a
    warning; the fallback is not represented as a production certification.
    """

    warnings: set[str] = set()
    try:
        params = json.loads(raw_params) if isinstance(raw_params, str) else dict(raw_params or {})
    except (json.JSONDecodeError, TypeError, ValueError):
        params = {}
        warnings.add("simulation_params_lookback_unreadable")
    try:
        metadata = get_registry().get_metadata(strategy_id)
        defaults = {
            item.name: item.default
            for item in getattr(metadata, "params", [])
            if item.default is not None
        }
        defaults.update(params)
        params = defaults
    except (KeyError, AttributeError):
        warnings.add("strategy_lookback_metadata_unavailable")
    candidates: list[int] = []
    for name, value in params.items():
        normalized = str(name).lower()
        if (
            any(marker in normalized for marker in _LOOKBACK_MARKERS)
            and not any(marker in normalized for marker in _NON_SIGNAL_WINDOWS)
            and isinstance(value, int)
            and not isinstance(value, bool)
            and value > 0
        ):
            candidates.append(value)
    if candidates:
        # Five prior sessions cover crossing/return comparisons around the
        # longest declared feature window without loading years of unrelated data.
        return min(max(candidates) + 5, 2_520), tuple(sorted(warnings))
    warnings.add("strategy_lookback_not_declared_using_252_sessions")
    return 252, tuple(sorted(warnings))


def _lookback_start(
    requested_date: str,
    strategy_id: str,
    raw_params: Any,
) -> tuple[str, tuple[str, ...]]:
    sessions, warnings = derive_simulation_lookback(strategy_id, raw_params)
    calendar_days = (sessions * 7 + 4) // 5 + 14
    return (
        (pd.Timestamp(requested_date) - pd.Timedelta(days=calendar_days)).strftime(
            "%Y-%m-%d"
        ),
        warnings,
    )


async def require_simulation_pit_readiness(
    *,
    user_id: int,
    start_date: str,
    end_date: str,
    portfolio_id: int | None = None,
) -> dict[str, Any]:
    """Verify every active deployment before a job or run row is created."""

    from backend.data.pit_runtime import (
        PitRuntimeDataError,
        require_pit_runtime_input,
    )
    from backend.services.experiment_eligibility import (
        PaperRiskBindingError,
        verify_paper_risk_binding,
    )

    pools: set[str] = set()
    trading_db = str(settings.abs_path(settings.TRADING_SIM_DB))
    experiment_db = str(settings.abs_path(settings.EXPERIMENT_DB))
    async with aiosqlite.connect(trading_db) as connection:
        connection.row_factory = aiosqlite.Row
        try:
            deployments = await _active_portfolio_deployments(
                connection,
                user_id=user_id,
                portfolio_id=portfolio_id,
            )
        except PortfolioSimulationBindingError as exc:
            raise PitRuntimeDataError(
                "paper_portfolio_binding_invalid",
                "模拟组合的部署绑定不可读取或引用了不可用部署",
            ) from exc
    source_ids = sorted(
        {
            int(row["source_experiment_id"])
            for row in deployments
            if row["source_experiment_id"] is not None
        }
    )
    source_pools: dict[int, str] = {}
    source_trust: dict[int, dict[str, Any]] = {}
    source_manifest_hashes: dict[int, str] = {}
    if source_ids:
        placeholders = ",".join("?" for _ in source_ids)
        async with aiosqlite.connect(experiment_db) as connection:
            connection.row_factory = aiosqlite.Row
            cursor = await connection.execute(
                f"SELECT e.id, e.pool_preset, e.strategy_id, "  # noqa: S608
                "m.schema_version, m.manifest_json, m.manifest_hash "
                "FROM experiments e LEFT JOIN research_run_manifests m "
                "ON m.experiment_id=e.id "
                f"WHERE e.id IN ({placeholders})",
                source_ids,
            )
            from backend.services.experiment_eligibility import (
                assess_experiment_eligibility,
            )

            for row in await cursor.fetchall():
                source_id = int(row["id"])
                source_pools[source_id] = str(row["pool_preset"] or "")
                source_manifest_hashes[source_id] = str(
                    row["manifest_hash"] or ""
                )
                eligibility = assess_experiment_eligibility(
                    experiment_id=source_id,
                    strategy_id=str(row["strategy_id"]),
                    schema_version=row["schema_version"],
                    manifest_json=row["manifest_json"],
                    manifest_hash=row["manifest_hash"],
                )
                if eligibility.eligible and row["manifest_json"]:
                    manifest = json.loads(row["manifest_json"])
                    trust = manifest.get("research_trust")
                    if (
                        isinstance(trust, dict)
                        and trust.get("profile") == "tushare_research_trusted"
                    ):
                        source_trust[source_id] = dict(trust)
    conditional_by_pool: dict[str, list[dict[str, Any]]] = {}
    strict_pools: set[str] = set()
    required_starts: dict[str, str] = {}
    readiness_warnings: set[str] = set()
    for row in deployments:
        deployment = dict(row)
        try:
            risk_snapshot = verify_paper_risk_binding(deployment)
        except PaperRiskBindingError as exc:
            raise PitRuntimeDataError(
                "paper_research_risk_binding_invalid",
                "模拟部署的数据代、来源、窗口或风险快照完整性校验失败",
            ) from exc
        source_id = row["source_experiment_id"]
        if risk_snapshot is not None:
            if source_id is None or risk_snapshot.get(
                "source_manifest_hash"
            ) != source_manifest_hashes.get(int(source_id)):
                raise PitRuntimeDataError(
                    "paper_source_manifest_binding_changed",
                    "模拟部署绑定的来源实验清单已变化，禁止继续运行",
                )
        pool = str(row["pool_preset"] or "")
        if not pool and row["source_experiment_id"] is not None:
            pool = source_pools.get(int(row["source_experiment_id"]), "")
        normalized_pool = POOL_NAME_ALIASES.get(
            pool or "csi300", pool or "csi300"
        )
        pools.add(normalized_pool)
        required_start, lookback_warnings = _lookback_start(
            start_date,
            str(row["strategy_id"] or ""),
            row["params"],
        )
        readiness_warnings.update(lookback_warnings)
        required_starts[normalized_pool] = min(
            required_starts.get(normalized_pool, required_start),
            required_start,
        )
        trust = source_trust.get(int(source_id)) if source_id is not None else None
        if trust is None:
            strict_pools.add(normalized_pool)
        else:
            conditional_by_pool.setdefault(normalized_pool, []).append(
                {
                    "trust": trust,
                    "generation_id": row["research_generation_id"],
                }
            )

    for pool in sorted(pools):
        lookback_start = required_starts[pool]
        bindings = conditional_by_pool.get(pool, [])
        if bindings:
            from backend.services.research_runtime import (
                ResearchRuntimeError,
                load_research_market,
            )

            for binding in bindings:
                generation_id = binding.get("generation_id")
                if generation_id:
                    try:
                        result = await load_research_market(
                            pool_id=pool,
                            required_start=lookback_start,
                            required_end=end_date,
                            generation_id=str(generation_id),
                        )
                    except ResearchRuntimeError as exc:
                        raise PitRuntimeDataError(exc.code, exc.message) from exc
                    readiness_warnings.update(
                        str(item)
                        for item in result["report"].get("warnings", [])
                    )
                    readiness_warnings.add(
                        "paper_execution_uses_adjusted_price_compatibility"
                    )
                    continue
                # Compatibility for deployments created before research
                # generation binding existed. They keep their legacy cache
                # contract rather than silently switching to the active store.
                from backend.services.tushare_research_trust import (
                    TushareResearchTrustError,
                    require_tushare_research_cache,
                )

                try:
                    await require_tushare_research_cache(
                        evidence_root=settings.abs_path(settings.PIT_EVIDENCE_DIR),
                        assessment=binding["trust"],
                        pool_id=pool,
                        required_start=lookback_start,
                        required_end=end_date,
                        purpose="execution_simulation",
                        require_benchmark=False,
                    )
                except TushareResearchTrustError as exc:
                    raise PitRuntimeDataError(
                        "tushare_conditional_simulation_not_ready",
                        str(exc),
                    ) from exc
        if pool in strict_pools or not bindings:
            await require_pit_runtime_input(
                pool_id=pool,
                required_start=lookback_start,
                required_end=end_date,
                purpose="execution",
                require_benchmark=False,
            )
    return {
        "schema_version": "paper-simulation-readiness/v2",
        "runnable": True,
        "required_starts": dict(sorted(required_starts.items())),
        "warnings": sorted(readiness_warnings),
        "live_eligible": False,
    }


def _order_intent_id(order: tuple[Any, ...], run_id: str) -> str:
    deployment_id, portfolio_id, trade_date, code, action = order[:5]
    payload = (
        f"{run_id}:{deployment_id}:{portfolio_id}:{trade_date}:"
        f"{code}:{action}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _valid_price(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result <= 0 or pd.isna(result):
        return None
    return result


def _field_prices(
    pivot: pd.DataFrame,
    trade_day: pd.Timestamp,
    field_name: str,
) -> dict[str, float]:
    row = pivot.loc[trade_day]
    if isinstance(row, pd.DataFrame):
        if field_name not in row.columns:
            return {}
        values = row[field_name].to_dict()
    elif isinstance(row.index, pd.MultiIndex):
        values = {
            str(code): value
            for (code, field), value in row.items()
            if str(field).lower() == field_name
        }
    else:
        values = row.to_dict() if field_name == "close" else {}
    return {
        str(code): price
        for code, value in values.items()
        if (price := _valid_price(value)) is not None
    }


async def _deployment_context(
    deployment: dict[str, Any],
) -> tuple[
    str,
    list[str],
    list[str],
    str | None,
    str | None,
    dict[str, Any] | None,
]:
    pool_id = deployment.get("pool_preset")
    custom_codes: list[str] = []
    industries: list[str] = []
    train_start = None
    train_end = None
    research_trust: dict[str, Any] | None = None
    source_experiment_id = deployment.get("source_experiment_id")
    if source_experiment_id:
        async with aiosqlite.connect(
            str(settings.abs_path(settings.EXPERIMENT_DB))
        ) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                """
                SELECT e.pool_preset, e.pool_custom_codes, e.pool_industries,
                       e.train_start, e.train_end, m.manifest_json
                FROM experiments e
                LEFT JOIN research_run_manifests m ON m.experiment_id=e.id
                WHERE e.id = ?
                """,
                (source_experiment_id,),
            )
            row = await cursor.fetchone()
            if row:
                pool_id = pool_id or row["pool_preset"]
                custom_codes = _parse_list(deployment.get("pool_custom_codes") or row["pool_custom_codes"])
                industries = _parse_list(deployment.get("pool_industries") or row["pool_industries"])
                train_start = row["train_start"]
                train_end = row["train_end"]
                if row["manifest_json"]:
                    manifest = json.loads(row["manifest_json"])
                    trust = manifest.get("research_trust")
                    if isinstance(trust, dict):
                        research_trust = dict(trust)
    return (
        POOL_NAME_ALIASES.get(pool_id or "csi300", pool_id or "csi300"),
        custom_codes,
        industries,
        train_start,
        train_end,
        research_trust,
    )


async def _load_deployed_model(
    strategy: Any,
    deployment: dict[str, Any],
) -> VerifiedModelArtifact | None:
    """Verify immutable evidence before any joblib/torch deserialization."""
    return await load_verified_deployment_model(strategy, deployment)


def _parse_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    text = str(value).strip()
    if not text:
        return []
    try:
        result = json.loads(text)
        if isinstance(result, list):
            return [str(item) for item in result]
    except json.JSONDecodeError:
        pass
    return [item.strip() for item in text.replace("，", ",").split(",") if item.strip()]


def _filter_codes(pivot: pd.DataFrame, codes: list[str]) -> pd.DataFrame:
    if not codes:
        return pivot
    selected = set(codes)
    if isinstance(pivot.columns, pd.MultiIndex):
        columns = [column for column in pivot.columns if str(column[0]) in selected]
    else:
        columns = [column for column in pivot.columns if str(column) in selected]
    return pivot.loc[:, columns]


async def _active_portfolio_deployments(
    connection: aiosqlite.Connection,
    *,
    user_id: int,
    portfolio_id: int | None,
) -> list[dict[str, Any]]:
    """Resolve normalized allocations, with a strict legacy-JSON fallback."""

    cursor = await connection.execute(
        """
        SELECT id, allocations, total_capital FROM portfolios
        WHERE user_id=? AND status='active' AND (? IS NULL OR id=?)
        ORDER BY id
        """,
        (user_id, portfolio_id, portfolio_id),
    )
    portfolios = [dict(row) for row in await cursor.fetchall()]
    if portfolio_id is not None and not portfolios:
        raise PortfolioSimulationScopeError(
            "portfolio_not_found_or_not_owned"
        )

    cursor = await connection.execute(
        """
        SELECT pa.portfolio_id AS allocation_portfolio_id, d.*
        FROM portfolio_allocations pa
        JOIN portfolios p ON p.id=pa.portfolio_id
        JOIN deployments d ON d.id=pa.deployment_id
        WHERE p.user_id=? AND p.status='active' AND d.status='active'
          AND (? IS NULL OR p.id=?)
        ORDER BY pa.portfolio_id, d.id
        """,
        (user_id, portfolio_id, portfolio_id),
    )
    deployments = [dict(row) for row in await cursor.fetchall()]
    normalized_portfolios = {
        int(row["allocation_portfolio_id"]) for row in deployments
    }
    for portfolio in portfolios:
        selected_portfolio_id = int(portfolio["id"])
        if selected_portfolio_id in normalized_portfolios:
            continue
        try:
            legacy = json.loads(portfolio.get("allocations") or "[]")
        except (json.JSONDecodeError, TypeError) as exc:
            raise PortfolioSimulationBindingError(
                "legacy_portfolio_allocations_invalid"
            ) from exc
        if not isinstance(legacy, list):
            raise PortfolioSimulationBindingError(
                "legacy_portfolio_allocations_invalid"
            )
        normalized, validation = canonicalize_allocations(
            legacy,
            float(portfolio["total_capital"]),
        )
        if not validation["valid"]:
            raise PortfolioSimulationBindingError(
                "legacy_portfolio_allocations_invalid"
            )
        for allocation in normalized:
            cursor = await connection.execute(
                """
                SELECT ? AS allocation_portfolio_id, d.*
                FROM deployments d
                WHERE d.id=? AND d.user_id=? AND d.status='active'
                """,
                (
                    selected_portfolio_id,
                    allocation["deployment_id"],
                    user_id,
                ),
            )
            deployment = await cursor.fetchone()
            if deployment is None:
                raise PortfolioSimulationBindingError(
                    "legacy_portfolio_deployment_unavailable"
                )
            deployments.append(dict(deployment))
    if not deployments:
        raise PortfolioSimulationBindingError(
            "portfolio_has_no_active_deployments"
        )
    return deployments


async def simulation_pool_bindings(
    user_id: int,
    portfolio_id: int | None = None,
) -> list[dict[str, str | None]]:
    """Return the actual pool/generation pairs used by active deployments."""

    trading_db = str(settings.abs_path(settings.TRADING_SIM_DB))
    async with aiosqlite.connect(trading_db) as connection:
        connection.row_factory = aiosqlite.Row
        rows = await _active_portfolio_deployments(
            connection,
            user_id=user_id,
            portfolio_id=portfolio_id,
        )
    unique: dict[tuple[str, str | None], dict[str, str | None]] = {}
    for row in rows:
        raw_pool = str(row["pool_preset"] or "csi300")
        pool_id = POOL_NAME_ALIASES.get(raw_pool, raw_pool)
        if pool_id == "custom":
            pool_id = "all_a"
        generation = (
            str(row["research_generation_id"])
            if row["research_generation_id"]
            else None
        )
        unique[(pool_id, generation)] = {
            "pool_id": pool_id,
            "generation_id": generation,
        }
    if not unique:
        raise PortfolioSimulationBindingError(
            "portfolio_has_no_active_deployments"
        )
    return list(unique.values())


async def _load_pivot(
    pool_id: str,
    requested_date: str,
    cache_by_pool: dict[str, pd.DataFrame],
    *,
    required_start: str | None = None,
    generation_id: str | None = None,
) -> pd.DataFrame:
    if pool_id == "custom":
        pool_id = "all_a"
    cache_identity = (
        f"research:{generation_id}:{pool_id}:{required_start}:{requested_date}"
        if generation_id
        else pool_id
    )
    if cache_identity not in cache_by_pool:
        if generation_id:
            from backend.services.research_runtime import load_research_market

            result = await load_research_market(
                pool_id=pool_id,
                required_start=required_start or requested_date,
                required_end=requested_date,
                generation_id=generation_id,
            )
            pivot = result["frame"]
            cache_by_pool[cache_identity] = pivot
            return pivot
        cache = DataCache()
        pivot = await cache.load_pivot(pool_id)
        if (
            pivot is None
            or pivot.empty
            or not has_price_field(pivot, "open")
        ):
            raise FileNotFoundError(
                f"股票池 {pool_id} 没有可用的本地 cache-only 行情"
            )
        if not isinstance(pivot.index, pd.DatetimeIndex):
            pivot.index = pd.to_datetime(pivot.index)
        pivot.sort_index(inplace=True)
        cache_by_pool[cache_identity] = pivot
    return cache_by_pool[cache_identity]


def _target_stock_weights(
    current_codes: set[str],
    signals: list[Any],
) -> dict[str, float] | None:
    """Turn event signals into a target universe.

    ``None`` means no event and therefore hold current positions unchanged.
    """
    if not signals:
        return None
    target_codes = set(current_codes)
    positive_hints: dict[str, float] = {}
    for signal in signals:
        action = signal.action.upper()
        if action == "SELL":
            target_codes.discard(signal.code)
        elif action == "BUY":
            target_codes.add(signal.code)
            positive_hints[signal.code] = max(float(signal.weight), 0.0)
    if not target_codes:
        return {}
    raw = {
        code: positive_hints.get(code, 0.0)
        for code in target_codes
    }
    if sum(raw.values()) <= 0:
        equal = 1 / len(target_codes)
        return {code: equal for code in sorted(target_codes)}
    # Existing positions without an explicit hint receive the average hint.
    hinted = [value for value in raw.values() if value > 0]
    fallback = sum(hinted) / len(hinted)
    raw = {code: value if value > 0 else fallback for code, value in raw.items()}
    total = sum(raw.values())
    return {code: value / total for code, value in raw.items()}


async def _run_all_portfolio_simulations(
    user_id: int,
    requested_date: str,
    shared_cache: dict[str, pd.DataFrame] | None,
) -> dict[str, Any]:
    """Fan out an all-portfolio request onto canonical portfolio/date scopes."""

    trading_db = str(settings.abs_path(settings.TRADING_SIM_DB))
    legacy_key = f"{user_id}:all:{requested_date}"
    async with aiosqlite.connect(trading_db) as connection:
        connection.row_factory = aiosqlite.Row
        await connection.execute("PRAGMA busy_timeout=5000")
        await connection.execute("BEGIN IMMEDIATE")
        cursor = await connection.execute(
            "SELECT * FROM simulation_runs WHERE idempotency_key=?",
            (legacy_key,),
        )
        legacy = await cursor.fetchone()
        if legacy is not None and legacy["status"] == "completed":
            await connection.rollback()
            return json.loads(legacy["summary"] or "{}")
        if legacy is not None:
            side_effects = await _simulation_side_effect_count(
                connection, str(legacy["id"])
            )
            if side_effects:
                await connection.rollback()
                raise RuntimeError(
                    "Legacy all-portfolio simulation has persisted side effects; "
                    "canonical per-portfolio replay is blocked"
                )
            if legacy["status"] == "running" and not _claim_is_expired(legacy):
                await connection.rollback()
                raise SimulationRunInProgressError(
                    "An identical simulation run is already in progress"
                )
            await connection.execute(
                """
                UPDATE simulation_runs
                SET status='failed', error='legacy_scope_replaced_after_expiry',
                    completed_at=datetime('now'), claim_expires_at=NULL
                WHERE id=?
                """,
                (legacy["id"],),
            )
        cursor = await connection.execute(
            """
            SELECT id FROM portfolios
            WHERE user_id=? AND status='active'
            ORDER BY id
            """,
            (user_id,),
        )
        portfolio_ids = [int(row["id"]) for row in await cursor.fetchall()]
        await connection.commit()

    results = [
        await run_daily_simulation(
            user_id,
            requested_date,
            shared_cache,
            portfolio_id=selected_portfolio_id,
        )
        for selected_portfolio_id in portfolio_ids
    ]
    if len(results) == 1:
        return results[0]
    return {
        "requested_date": requested_date,
        "portfolio_runs": results,
        "portfolios": [
            portfolio
            for result in results
            for portfolio in result.get("portfolios", [])
        ],
        "signals": sum(int(result.get("signals") or 0) for result in results),
        "orders": sum(int(result.get("orders") or 0) for result in results),
        "research_warnings": sorted(
            {
                str(warning)
                for result in results
                for warning in result.get("research_warnings", [])
            }
        ),
        "live_eligible": False,
    }


async def run_daily_simulation(
    user_id: int,
    requested_date: str | None = None,
    shared_cache: dict[str, pd.DataFrame] | None = None,
    portfolio_id: int | None = None,
) -> dict[str, Any]:
    """Run one idempotent EOD paper-trading cycle for one or all portfolios."""
    if requested_date is None:
        from backend.data.pit_runtime import PitRuntimeDataError

        raise PitRuntimeDataError(
            "pit_simulation_date_required",
            "PIT-only 模拟必须显式绑定已完成交易日，禁止从旧缓存推断日期",
        )
    # Validate the public date contract early.
    pd.Timestamp(requested_date)
    if portfolio_id is None:
        return await _run_all_portfolio_simulations(user_id, requested_date, shared_cache)
    readiness = await require_simulation_pit_readiness(
        user_id=user_id,
        start_date=requested_date,
        end_date=requested_date,
        portfolio_id=portfolio_id,
    )
    run_id = uuid.uuid4().hex
    claim_token = uuid.uuid4().hex
    idempotency_key = f"{user_id}:{portfolio_id}:{requested_date}"
    trading_db = str(settings.abs_path(settings.TRADING_SIM_DB))

    async with aiosqlite.connect(trading_db) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute("PRAGMA busy_timeout=5000")
        await conn.execute("BEGIN IMMEDIATE")
        cursor = await conn.execute(
            "SELECT * FROM simulation_runs WHERE idempotency_key = ?",
            (idempotency_key,),
        )
        existing = await cursor.fetchone()
        if existing and existing["status"] == "completed":
            await conn.rollback()
            return json.loads(existing["summary"] or "{}")
        if (
            existing
            and existing["status"] == "running"
            and not _claim_is_expired(existing)
        ):
            await conn.rollback()
            raise SimulationRunInProgressError(
                "An identical simulation run is already in progress"
            )
        if existing is None:
            cursor = await conn.execute(
                """
                SELECT MAX(trade_date) AS latest_trade_date
                FROM simulation_runs
                WHERE user_id=? AND portfolio_id IS ? AND status='completed'
                """,
                (user_id, portfolio_id),
            )
            latest = await cursor.fetchone()
            latest_trade_date = latest["latest_trade_date"] if latest else None
            if latest_trade_date and requested_date < latest_trade_date:
                raise ValueError(
                    f"不能在已完成 {latest_trade_date} 后回补更早的模拟交易日 "
                    f"{requested_date}"
                )
        if existing:
            run_id = existing["id"]
            side_effects = await _simulation_side_effect_count(conn, run_id)
            if side_effects:
                await conn.rollback()
                raise RuntimeError(
                    "A failed simulation run already has persisted side "
                    "effects and cannot be retried automatically"
                )
            await conn.execute(
                """
                UPDATE simulation_runs
                SET status='running', error=NULL, completed_at=NULL,
                    claim_token=?, claim_expires_at=?, heartbeat_at=datetime('now')
                WHERE id=?
                """,
                (claim_token, _claim_expiry(), run_id),
            )
        else:
            await conn.execute(
                """
                INSERT INTO simulation_runs
                    (id, user_id, portfolio_id, trade_date, idempotency_key,
                     status, claim_token, claim_expires_at, heartbeat_at)
                VALUES (?, ?, ?, ?, ?, 'running', ?, ?, datetime('now'))
                """,
                (
                    run_id,
                    user_id,
                    portfolio_id,
                    requested_date,
                    idempotency_key,
                    claim_token,
                    _claim_expiry(),
                ),
            )
        await conn.commit()

    cache_by_pool: dict[str, pd.DataFrame] = shared_cache if shared_cache is not None else {}
    summary: dict[str, Any] = {
        "simulation_run_id": run_id,
        "requested_date": requested_date,
        "portfolios": [],
        "signals": 0,
        "orders": 0,
        "research_warnings": list((readiness or {}).get("warnings", [])),
        "live_eligible": False,
    }
    published_signals: dict[int, list[dict[str, Any]]] = {}
    verified_deployments: dict[int, dict[str, Any]] = {}

    try:
        async with aiosqlite.connect(trading_db) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA foreign_keys=ON")
            await conn.execute("PRAGMA busy_timeout=5000")
            cursor = await conn.execute(
                """
                SELECT * FROM portfolios
                WHERE user_id = ? AND status = 'active'
                  AND (? IS NULL OR id = ?)
                ORDER BY id
                """,
                (user_id, portfolio_id, portfolio_id),
            )
            portfolios = [dict(row) for row in await cursor.fetchall()]
            if portfolio_id is not None and not portfolios:
                raise ValueError(f"Portfolio {portfolio_id} is not active or does not belong to this user")

            for portfolio in portfolios:
                cursor = await conn.execute(
                    """
                    SELECT pa.*, d.*
                    FROM portfolio_allocations pa
                    JOIN deployments d ON d.id = pa.deployment_id
                    WHERE pa.portfolio_id = ? AND d.user_id = ? AND d.status = 'active'
                    ORDER BY pa.deployment_id
                    """,
                    (portfolio["id"], user_id),
                )
                allocation_rows = [dict(row) for row in await cursor.fetchall()]
                if not allocation_rows:
                    legacy = json.loads(portfolio.get("allocations") or "[]")
                    normalized, validation = canonicalize_allocations(
                        legacy,
                        float(portfolio["total_capital"]),
                    )
                    if not validation["valid"]:
                        raise ValueError("; ".join(validation["errors"]))
                    deployments: list[dict[str, Any]] = []
                    for item in normalized:
                        cursor = await conn.execute(
                            """
                            SELECT * FROM deployments
                            WHERE id = ? AND user_id = ? AND status = 'active'
                            """,
                            (item["deployment_id"], user_id),
                        )
                        deployment = await cursor.fetchone()
                        if deployment:
                            deployments.append({**dict(deployment), **item})
                    allocation_rows = deployments

                for deployment in allocation_rows:
                    if deployment.get("promotion_binding_hash"):
                        await verify_deployment_promotion(deployment)
                    deployment_id = int(
                        deployment.get("deployment_id") or deployment["id"]
                    )
                    verified_deployments[deployment_id] = deployment

                cursor = await conn.execute(
                    """
                    SELECT * FROM position_snapshots
                    WHERE portfolio_id = ? AND date = (
                        SELECT MAX(date) FROM position_snapshots
                        WHERE portfolio_id = ? AND date < ?
                    )
                    """,
                    (portfolio["id"], portfolio["id"], requested_date),
                )
                current_rows = [dict(row) for row in await cursor.fetchall()]
                positions: dict[tuple[int, str], dict[str, Any]] = {
                    (int(row["deployment_id"]), row["code"]): row
                    for row in current_rows
                    if row.get("deployment_id") is not None and row["shares"] > 0
                }
                cash = (
                    float(portfolio["cash_balance"])
                    if portfolio.get("cash_balance") is not None
                    else float(portfolio["total_capital"])
                    - sum(float(row["market_value"]) for row in current_rows)
                )
                portfolio_equity_base = cash + sum(
                    float(row["market_value"]) for row in current_rows
                )
                effective_dates: set[str] = set()
                signal_rows: list[tuple[Any, ...]] = []
                order_rows: list[tuple[Any, ...]] = []
                latest_prices: dict[tuple[int, str], float] = {}
                cost_model = CostModel()
                strategy_states: dict[int, dict[str, float]] = {}
                cursor = await conn.execute(
                    """
                    SELECT sn.* FROM strategy_nav_history sn
                    JOIN (
                        SELECT deployment_id, MAX(date) AS latest_date
                        FROM strategy_nav_history
                        WHERE portfolio_id=? AND date < ?
                        GROUP BY deployment_id
                    ) latest
                      ON latest.deployment_id=sn.deployment_id
                     AND latest.latest_date=sn.date
                    WHERE sn.portfolio_id=?
                    """,
                    (portfolio["id"], requested_date, portfolio["id"]),
                )
                prior_strategy_nav = {
                    int(row["deployment_id"]): dict(row)
                    for row in await cursor.fetchall()
                }

                for deployment in allocation_rows:
                    deployment_id = int(deployment["deployment_id"] if "deployment_id" in deployment else deployment["id"])
                    (
                        pool_id,
                        custom_codes,
                        industries,
                        train_start,
                        train_end,
                        research_trust,
                    ) = await _deployment_context(deployment)
                    deployment_lookback_start, lookback_warnings = _lookback_start(
                        requested_date,
                        str(deployment["strategy_id"]),
                        deployment.get("params"),
                    )
                    bound_generation_id = deployment.get(
                        "research_generation_id"
                    )
                    if bound_generation_id:
                        pivot = await _load_pivot(
                            pool_id,
                            requested_date,
                            cache_by_pool,
                            required_start=deployment_lookback_start,
                            generation_id=str(bound_generation_id),
                        )
                    else:
                        pivot = await _load_pivot(
                            pool_id,
                            requested_date,
                            cache_by_pool,
                        )
                    summary["research_warnings"] = sorted(
                        {
                            *summary["research_warnings"],
                            *lookback_warnings,
                        }
                    )
                    if research_trust is not None and not bound_generation_id:
                        from backend.data.point_in_time_universe import (
                            mask_market_data_to_timeline,
                        )
                        from backend.services.tushare_research_trust import (
                            assess_tushare_research_trust,
                            build_tushare_research_timeline,
                            load_tushare_backfill_report,
                        )

                        evidence = research_trust.get("evidence")
                        if not isinstance(evidence, dict):
                            raise ValueError("Tushare 条件模拟缺少候选报告证据")
                        report_digest = str(
                            evidence.get("candidate_report_sha256") or ""
                        )
                        start = max(
                            pd.Timestamp(pivot.index.min()),
                            pd.Timestamp(deployment_lookback_start),
                        ).strftime("%Y-%m-%d")
                        end = requested_date
                        pivot = pivot.loc[start:end]
                        evidence_root = settings.abs_path(settings.PIT_EVIDENCE_DIR)
                        current_trust = assess_tushare_research_trust(
                            report=load_tushare_backfill_report(
                                evidence_root, report_digest
                            ),
                            report_object_sha256=report_digest,
                            required_start=start,
                            required_end=end,
                            purpose="execution_simulation",
                        )
                        if current_trust.get("eligible") is not True:
                            raise ValueError(
                                "Tushare 条件模拟窗口没有完整历史成分证据"
                            )
                        timeline = build_tushare_research_timeline(
                            evidence_root=evidence_root,
                            assessment=current_trust,
                            pool_id=pool_id,
                            trading_dates=pivot.index,
                        )
                        pivot = mask_market_data_to_timeline(pivot, timeline)
                    pivot = _filter_codes(pivot, custom_codes)
                    if industries:
                        if isinstance(pivot.columns, pd.MultiIndex):
                            universe_codes = sorted({str(column[0]) for column in pivot.columns})
                        else:
                            universe_codes = [str(column) for column in pivot.columns]
                        manager = UniverseManager(
                            None,
                            DataCache(),
                        )
                        industry_codes = await manager.filter_by_industry(
                            universe_codes,
                            industries,
                        )
                        pivot = _filter_codes(pivot, industry_codes)
                    if pivot.empty or len(pivot.columns) == 0:
                        raise ValueError(f"部署 {deployment_id} 的股票池筛选结果为空")
                    requested_timestamp = pd.Timestamp(requested_date)
                    available_days = pivot.index[pivot.index <= requested_timestamp]
                    if requested_timestamp not in pivot.index:
                        raise ValueError(
                            f"{requested_date} 不是部署 {deployment_id} 的可执行交易日，"
                            "模拟盘不会回退到前一交易日"
                        )
                    if len(available_days) < 2:
                        raise ValueError(
                            f"部署 {deployment_id} 在 {requested_date} 前没有足够行情执行 T+1 信号"
                        )
                    trade_day = requested_timestamp
                    signal_day = available_days[-2]
                    trade_date = trade_day.strftime("%Y-%m-%d")
                    signal_date = signal_day.strftime("%Y-%m-%d")
                    effective_dates.add(trade_date)
                    execution_prices = _field_prices(pivot, trade_day, "open")
                    close_prices = _field_prices(pivot, trade_day, "close")
                    for code, price in close_prices.items():
                        latest_prices[(deployment_id, code)] = price

                    params = json.loads(deployment.get("params") or "{}")
                    if train_start:
                        params["_train_start"] = train_start
                    if train_end:
                        params["_train_end"] = min(train_end, signal_date)
                    strategy = get_registry().create_strategy(deployment["strategy_id"])
                    from backend.strategies.base import TrainableStrategy
                    from backend.strategies.research_context import (
                        validate_strategy_research_context,
                    )

                    metadata_factory = getattr(strategy, "metadata", None)
                    metadata = (
                        metadata_factory()
                        if callable(metadata_factory)
                        else None
                    )
                    validate_strategy_research_context(
                        requires_training=bool(
                            getattr(metadata, "requires_training", False)
                        ),
                        trainable_protocol=isinstance(
                            strategy,
                            TrainableStrategy,
                        ),
                        context=None,
                        point_in_time_capability=(
                            getattr(
                                strategy,
                                "point_in_time_context_capability",
                                None,
                            )
                        ),
                    )
                    await _load_deployed_model(
                        strategy,
                        deployment,
                    )
                    signal_pivot = pivot.loc[pivot.index <= signal_day]
                    generation_start = max(
                        signal_pivot.index.min(),
                        pd.Timestamp(deployment_lookback_start),
                    ).strftime("%Y-%m-%d")
                    signals_by_date = await asyncio.to_thread(
                        strategy.generate_batch_signals,
                        signal_pivot,
                        params,
                        generation_start,
                        signal_date,
                    )
                    day_signals = signals_by_date.get(signal_date, [])
                    current_codes = {
                        code for dep_id, code in positions if dep_id == deployment_id
                    }
                    target_weights = _target_stock_weights(current_codes, day_signals)

                    for signal in day_signals:
                        weight = float(signal.weight)
                        target_bps = int(round(max(weight, 0.0) * 10_000)) if weight <= 1 else 0
                        signal_rows.append(
                            (
                                deployment_id,
                                signal_date,
                                signal.code,
                                signal.action.upper(),
                                float(signal.score),
                                weight,
                                0.0,
                                "策略日频信号",
                                run_id,
                                target_bps,
                            )
                        )
                    if day_signals:
                        published_signals[deployment_id] = [
                            {
                                "deployment_id": deployment_id,
                                "date": signal_date,
                                "code": signal.code,
                                "action": signal.action.upper(),
                                "score": float(signal.score),
                                "weight": float(signal.weight),
                                "confidence": 0.0,
                                "reasoning": "策略日频信号",
                            }
                            for signal in day_signals
                        ]

                    strategy_bps = int(deployment.get("target_weight_bps") or 0)
                    strategy_capital = portfolio_equity_base * strategy_bps / 10_000
                    prior_nav = prior_strategy_nav.get(deployment_id)
                    opening_equity = float(prior_nav["total_equity"]) if prior_nav else 0.0
                    net_flow = strategy_capital - opening_equity
                    strategy_state = {
                        "opening_equity": opening_equity,
                        "net_flow": net_flow,
                        "cash": (float(prior_nav["cash_balance"]) if prior_nav else 0.0)
                        + net_flow,
                        "realized_pnl": 0.0,
                        "transaction_cost": 0.0,
                        "turnover": 0.0,
                        "prior_cumulative_return": (
                            float(prior_nav["cumulative_return"] or 0.0)
                            if prior_nav else 0.0
                        ),
                    }
                    strategy_states[deployment_id] = strategy_state
                    if target_weights is None:
                        continue
                    target_shares: dict[str, int] = {}
                    for code, weight in target_weights.items():
                        price = execution_prices.get(code)
                        if price:
                            target_shares[code] = cost_model.calc_shares(strategy_capital * weight, price)
                        elif code not in current_codes:
                            order_rows.append(
                                (
                                    deployment_id, portfolio["id"], trade_date, code, "BUY",
                                    0.0, 0, 0.0, 0.0,
                                    deployment["strategy_id"], 0.0, "market", "rejected",
                                    "缺少 T+1 开盘价", run_id, None,
                                )
                            )
                    all_codes = current_codes | set(target_shares)
                    deltas = {
                        code: target_shares.get(code, 0)
                        - int(positions.get((deployment_id, code), {}).get("shares", 0))
                        for code in all_codes
                    }

                    # Sells settle first so their proceeds can finance buys.
                    for code, delta in sorted(deltas.items(), key=lambda item: item[1]):
                        if delta >= 0:
                            continue
                        current = positions[(deployment_id, code)]
                        shares = min(-delta, int(current["shares"]))
                        price = execution_prices.get(code)
                        if not price:
                            order_rows.append(
                                (
                                    deployment_id, portfolio["id"], trade_date, code, "SELL",
                                    0.0, shares, 0.0, 0.0,
                                    deployment["strategy_id"], 0.0, "market", "rejected",
                                    "缺少 T+1 开盘价", run_id, None,
                                )
                            )
                            continue
                        if shares <= 0:
                            continue
                        proceeds = cost_model.calc_sell_cost(price, shares)
                        cost = price * shares - proceeds
                        cash += proceeds
                        strategy_state["cash"] += proceeds
                        strategy_state["realized_pnl"] += (
                            proceeds - float(current["avg_cost"]) * shares
                        )
                        strategy_state["transaction_cost"] += cost
                        strategy_state["turnover"] += price * shares
                        remaining = int(current["shares"]) - shares
                        if remaining:
                            current["shares"] = remaining
                        else:
                            positions.pop((deployment_id, code), None)
                        order_rows.append(
                            (
                                deployment_id, portfolio["id"], trade_date, code, "SELL",
                                price, shares, price * shares, cost,
                                deployment["strategy_id"], 0.0, "market", "filled",
                                "", run_id, f"{trade_date} 09:30:00",
                            )
                        )

                    for code, delta in sorted(deltas.items(), key=lambda item: item[1], reverse=True):
                        if delta <= 0:
                            continue
                        price = execution_prices.get(code)
                        if not price:
                            continue
                        shares = min(delta, cost_model.calc_shares(cash, price))
                        if shares < 100:
                            continue
                        total_cost = cost_model.calc_buy_cost(price, shares)
                        cash -= total_cost
                        strategy_state["cash"] -= total_cost
                        strategy_state["transaction_cost"] += total_cost - price * shares
                        strategy_state["turnover"] += price * shares
                        key = (deployment_id, code)
                        current = positions.get(key)
                        old_shares = int(current["shares"]) if current else 0
                        old_cost = float(current["avg_cost"]) if current else 0.0
                        new_shares = old_shares + shares
                        positions[key] = {
                            "deployment_id": deployment_id,
                            "code": code,
                            "shares": new_shares,
                            "avg_cost": (old_cost * old_shares + total_cost) / new_shares,
                            "close_price": price,
                        }
                        order_rows.append(
                            (
                                deployment_id, portfolio["id"], trade_date, code, "BUY",
                                price, shares, price * shares, total_cost - price * shares,
                                deployment["strategy_id"], 0.0, "market", "filled",
                                "", run_id, f"{trade_date} 09:30:00",
                            )
                        )

                if not effective_dates:
                    summary["portfolios"].append(
                        {"portfolio_id": portfolio["id"], "status": "no_active_deployments"}
                    )
                    continue
                snapshot_date = max(effective_dates)
                if signal_rows:
                    await conn.executemany(
                        """
                        INSERT INTO daily_signals
                            (deployment_id, date, code, action, score, weight,
                             confidence, reasoning, simulation_run_id, target_weight_bps)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(deployment_id, date, code) DO UPDATE SET
                            action=excluded.action, score=excluded.score,
                            weight=excluded.weight, confidence=excluded.confidence,
                            reasoning=excluded.reasoning,
                            simulation_run_id=excluded.simulation_run_id,
                            target_weight_bps=excluded.target_weight_bps
                        """,
                        signal_rows,
                    )
                if order_rows:
                    order_rows_with_intent = [
                        (*row, _order_intent_id(row, run_id))
                        for row in order_rows
                    ]
                    await conn.executemany(
                        """
                        INSERT INTO orders
                            (deployment_id, portfolio_id, date, code, action, price,
                             shares, amount, cost, signal_strategy, signal_score,
                             order_type, status, reject_reason, simulation_run_id,
                             filled_at, order_intent_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        order_rows_with_intent,
                    )

                snapshot_rows: list[tuple[Any, ...]] = []
                total_market_value = 0.0
                for (deployment_id, code), position in positions.items():
                    price = latest_prices.get((deployment_id, code)) or _valid_price(position.get("close_price"))
                    if not price:
                        continue
                    shares = int(position["shares"])
                    market_value = price * shares
                    total_market_value += market_value
                    snapshot_rows.append(
                        (
                            portfolio["id"], deployment_id, snapshot_date, code, shares,
                            float(position["avg_cost"]), price, market_value,
                            (price - float(position["avg_cost"])) * shares,
                            run_id, 0.0,
                        )
                    )
                total_equity = cash + total_market_value
                snapshot_rows = [
                    (*row[:-1], (row[7] / total_equity * 100 if total_equity > 0 else 0.0))
                    for row in snapshot_rows
                ]
                if snapshot_rows:
                    await conn.executemany(
                        """
                        INSERT INTO position_snapshots
                            (portfolio_id, deployment_id, date, code, shares, avg_cost,
                             close_price, market_value, unrealized_pnl,
                             simulation_run_id, weight_in_portfolio)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(portfolio_id, deployment_id, date, code) DO UPDATE SET
                            shares=excluded.shares, avg_cost=excluded.avg_cost,
                            close_price=excluded.close_price,
                            market_value=excluded.market_value,
                            unrealized_pnl=excluded.unrealized_pnl,
                            simulation_run_id=excluded.simulation_run_id,
                            weight_in_portfolio=excluded.weight_in_portfolio
                        """,
                        snapshot_rows,
                    )
                strategy_market_values: dict[int, float] = {}
                strategy_unrealized: dict[int, float] = {}
                for row in snapshot_rows:
                    deployment_id = int(row[1])
                    strategy_market_values[deployment_id] = (
                        strategy_market_values.get(deployment_id, 0.0) + float(row[7])
                    )
                    strategy_unrealized[deployment_id] = (
                        strategy_unrealized.get(deployment_id, 0.0) + float(row[8])
                    )
                cursor = await conn.execute(
                    """
                    SELECT nav FROM nav_history
                    WHERE portfolio_id = ? AND date < ?
                    ORDER BY date DESC LIMIT 1
                    """,
                    (portfolio["id"], snapshot_date),
                )
                previous = await cursor.fetchone()
                previous_nav = float(previous["nav"]) if previous else float(portfolio["total_capital"])
                daily_return = total_equity / previous_nav - 1 if previous_nav else 0.0
                cumulative_return = total_equity / float(portfolio["total_capital"]) - 1
                portfolio_daily_pnl = total_equity - previous_nav
                strategy_nav_rows: list[tuple[Any, ...]] = []
                for deployment in allocation_rows:
                    deployment_id = int(deployment.get("deployment_id", deployment.get("id")))
                    state = strategy_states.get(deployment_id)
                    if state is None:
                        continue
                    opening_equity = state["opening_equity"]
                    net_flow = state["net_flow"]
                    market_value = strategy_market_values.get(deployment_id, 0.0)
                    strategy_equity = state["cash"] + market_value
                    strategy_pnl = strategy_equity - opening_equity - net_flow
                    return_base = opening_equity + net_flow
                    strategy_return = strategy_pnl / return_base if return_base else 0.0
                    strategy_cumulative = (
                        (1.0 + state["prior_cumulative_return"])
                        * (1.0 + strategy_return) - 1.0
                    )
                    turnover_rate = state["turnover"] / return_base if return_base else 0.0
                    contribution_return = strategy_pnl / previous_nav if previous_nav else 0.0
                    strategy_nav_rows.append(
                        (
                            portfolio["id"], deployment_id, snapshot_date,
                            opening_equity, net_flow, state["cash"], market_value,
                            strategy_equity, strategy_pnl, strategy_return,
                            strategy_cumulative, state["realized_pnl"],
                            strategy_unrealized.get(deployment_id, 0.0),
                            state["transaction_cost"], state["turnover"],
                            turnover_rate, strategy_pnl, contribution_return, run_id,
                        )
                    )
                if strategy_nav_rows:
                    await conn.executemany(
                        """
                        INSERT INTO strategy_nav_history
                            (portfolio_id, deployment_id, date, opening_equity,
                             net_flow, cash_balance, market_value, total_equity,
                             daily_pnl, daily_return, cumulative_return,
                             realized_pnl, unrealized_pnl, transaction_cost,
                             turnover, turnover_rate, contribution_pnl,
                             contribution_return, simulation_run_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(portfolio_id, deployment_id, date) DO UPDATE SET
                            opening_equity=excluded.opening_equity,
                            net_flow=excluded.net_flow,
                            cash_balance=excluded.cash_balance,
                            market_value=excluded.market_value,
                            total_equity=excluded.total_equity,
                            daily_pnl=excluded.daily_pnl,
                            daily_return=excluded.daily_return,
                            cumulative_return=excluded.cumulative_return,
                            realized_pnl=excluded.realized_pnl,
                            unrealized_pnl=excluded.unrealized_pnl,
                            transaction_cost=excluded.transaction_cost,
                            turnover=excluded.turnover,
                            turnover_rate=excluded.turnover_rate,
                            contribution_pnl=excluded.contribution_pnl,
                            contribution_return=excluded.contribution_return,
                            simulation_run_id=excluded.simulation_run_id
                        """,
                        strategy_nav_rows,
                    )
                await conn.execute(
                    """
                    INSERT INTO nav_history
                        (portfolio_id, deployment_id, date, nav, daily_return,
                         cumulative_return, simulation_run_id, cash_balance, total_equity)
                    VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(portfolio_id, date) DO UPDATE SET
                        nav=excluded.nav, daily_return=excluded.daily_return,
                        cumulative_return=excluded.cumulative_return,
                        simulation_run_id=excluded.simulation_run_id,
                        cash_balance=excluded.cash_balance,
                        total_equity=excluded.total_equity
                    """,
                    (
                        portfolio["id"], snapshot_date, total_equity, daily_return,
                        cumulative_return, run_id, cash, total_equity,
                    ),
                )
                await conn.execute(
                    """
                    UPDATE portfolios
                    SET cash_balance=?, updated_at=datetime('now')
                    WHERE id=?
                    """,
                    (cash, portfolio["id"]),
                )
                summary["signals"] += len(signal_rows)
                summary["orders"] += len(order_rows)
                summary["portfolios"].append(
                    {
                        "portfolio_id": portfolio["id"],
                        "trade_date": snapshot_date,
                        "total_equity": round(total_equity, 2),
                        "cash_balance": round(cash, 2),
                        "positions": len(snapshot_rows),
                        "orders": len(order_rows),
                        "strategy_attribution_pnl": round(
                            sum(row[8] for row in strategy_nav_rows), 2
                        ),
                        "portfolio_daily_pnl": round(portfolio_daily_pnl, 2),
                    }
                )

            # Close the approval/revocation TOCTOU window before committing any
            # generated signals, orders, or portfolio state.
            for deployment in verified_deployments.values():
                if deployment.get("promotion_binding_hash"):
                    await verify_deployment_promotion(deployment)

            cursor = await conn.execute(
                """
                UPDATE simulation_runs
                SET status='completed', summary=?, completed_at=datetime('now'),
                    claim_expires_at=NULL, heartbeat_at=datetime('now')
                WHERE id=? AND status='running' AND claim_token=?
                """,
                (
                    json.dumps(summary, ensure_ascii=False),
                    run_id,
                    claim_token,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    "Simulation run ownership changed before commit"
                )
            await conn.commit()

        # WebSocket publication is best-effort; REST remains the source of truth.
        try:
            from backend.ws.realtime import publish_realtime_signal

            for deployment_id, signals in published_signals.items():
                await publish_realtime_signal(
                    deployment_id,
                    {
                        "type": "signal_batch",
                        "deployment_id": deployment_id,
                        "signals": signals,
                    },
                )
        except Exception:
            pass
        return summary
    except Exception as exc:
        async with aiosqlite.connect(trading_db) as conn:
            await conn.execute("PRAGMA busy_timeout=5000")
            await conn.execute(
                """
                UPDATE simulation_runs
                SET status='failed', error=?, completed_at=datetime('now'),
                    claim_expires_at=NULL, heartbeat_at=datetime('now')
                WHERE id=? AND status='running' AND claim_token=?
                """,
                (str(exc), run_id, claim_token),
            )
            await conn.commit()
        raise


async def run_simulation_backfill(
    user_id: int,
    start_date: str,
    end_date: str,
    progress_callback: Callable[[float], Awaitable[None]] | None = None,
    portfolio_id: int | None = None,
    restart: bool = False,
) -> dict[str, Any]:
    """Replay dates executable by every pool in the selected portfolio."""
    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date)
    if end < start:
        raise ValueError("end_date must not be earlier than start_date")

    shared_cache: dict[str, pd.DataFrame] = {}
    bindings = await simulation_pool_bindings(user_id, portfolio_id)
    date_sets: list[set[str]] = []
    for binding in bindings:
        if binding["generation_id"]:
            pivot = await _load_pivot(
                str(binding["pool_id"]),
                end_date,
                shared_cache,
                required_start=start_date,
                generation_id=binding["generation_id"],
            )
        else:
            pivot = await _load_pivot(
                str(binding["pool_id"]), end_date, shared_cache
            )
        date_sets.append(
            {
                value.strftime("%Y-%m-%d")
                for value in pivot.index[
                    (pivot.index >= start) & (pivot.index <= end)
                ]
            }
        )
    dates = sorted(set.intersection(*date_sets)) if date_sets else []
    if not dates:
        raise ValueError(
            "No common executable trading dates are available for the portfolio pools"
        )
    if len(dates) > 750:
        raise ValueError("A single replay may contain at most 750 trading days")

    if restart:
        if portfolio_id is None:
            raise ValueError("portfolio_id is required when restarting a simulation")
        await reset_portfolio_simulation(user_id, portfolio_id)

    completed: list[dict[str, Any]] = []
    for index, trade_date in enumerate(dates, start=1):
        result = await run_daily_simulation(
            user_id,
            trade_date,
            shared_cache,
            portfolio_id=portfolio_id,
        )
        completed.append(result)
        if progress_callback:
            await progress_callback(index / len(dates))
    return {
        "start_date": dates[0],
        "end_date": dates[-1],
        "trading_days": len(dates),
        "portfolio_id": portfolio_id,
        "pool_ids": sorted({str(item["pool_id"]) for item in bindings}),
        "restarted": restart,
        "last_result": completed[-1],
    }


async def reset_portfolio_simulation(user_id: int, portfolio_id: int) -> None:
    """Clear generated paper-trading state for exactly one owned portfolio."""
    trading_db = str(settings.abs_path(settings.TRADING_SIM_DB))
    async with aiosqlite.connect(trading_db) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys=ON")
        cursor = await conn.execute(
            "SELECT total_capital FROM portfolios WHERE id=? AND user_id=?",
            (portfolio_id, user_id),
        )
        portfolio = await cursor.fetchone()
        if portfolio is None:
            raise ValueError(f"Portfolio {portfolio_id} does not belong to this user")

        await conn.execute("DELETE FROM orders WHERE portfolio_id=?", (portfolio_id,))
        await conn.execute(
            "DELETE FROM position_snapshots WHERE portfolio_id=?",
            (portfolio_id,),
        )
        await conn.execute("DELETE FROM nav_history WHERE portfolio_id=?", (portfolio_id,))
        await conn.execute(
            "DELETE FROM strategy_nav_history WHERE portfolio_id=?",
            (portfolio_id,),
        )
        await conn.execute(
            "DELETE FROM simulation_runs WHERE user_id=? AND portfolio_id=?",
            (user_id, portfolio_id),
        )
        await conn.execute(
            """
            UPDATE portfolios
            SET cash_balance=total_capital, updated_at=datetime('now')
            WHERE id=? AND user_id=?
            """,
            (portfolio_id, user_id),
        )
        await conn.commit()
