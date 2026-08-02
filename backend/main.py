"""FastAPI 应用入口 —— 量化验证平台 V3."""

from __future__ import annotations

# Windows/macOS 上 LightGBM 与 PyArrow 的原生库对加载顺序敏感。进程入口必须
# 先于 pandas（以及任何可能间接加载 PyArrow 的后端模块）预加载 LightGBM。
from backend.version import (
    APP_COMMIT,
    APP_VERSION,
    RUNTIME_STARTED_AT,
    runtime_code_evidence,
)
from backend.strategies.ml.runtime import preload_frame_safe_lightgbm

preload_frame_safe_lightgbm()

import asyncio
import logging
import sqlite3
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from backend.config import settings
from backend.core.log_redaction import install_sensitive_url_log_filter
from backend.core.request_id import attach_request_id
from backend.jobs.observability import structured_log
install_sensitive_url_log_filter()
logger = logging.getLogger("quant_platform")


# ═══════════════════════════════════════════════════════════════════════════
# 数据库初始化 SQL（来自架构文档第7节）
# ═══════════════════════════════════════════════════════════════════════════

_USERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    display_name TEXT,
    email TEXT,
    is_admin INTEGER DEFAULT 0,
    is_active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now')),
    last_login TEXT
);

CREATE TABLE IF NOT EXISTS user_permissions (
    user_id INTEGER NOT NULL,
    permission TEXT NOT NULL,
    granted_by INTEGER,
    granted_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, permission),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS user_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    token_jti TEXT UNIQUE NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);
"""

_EXPERIMENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT,
    strategy_id TEXT NOT NULL,
    strategy_category TEXT NOT NULL,
    is_starred INTEGER DEFAULT 0,
    labels TEXT,
    pool_preset TEXT,
    pool_custom_codes TEXT,
    pool_industries TEXT,
    train_start TEXT,
    train_end TEXT,
    test_start TEXT NOT NULL,
    test_end TEXT NOT NULL,
    params TEXT NOT NULL,
    params_hash TEXT NOT NULL,
    mode TEXT DEFAULT 'batch',
    requires_training INTEGER DEFAULT 0,
    retrain_frequency TEXT,
    status TEXT DEFAULT 'pending',
    error_log TEXT,
    ai_diagnosis TEXT,
    progress_pct REAL DEFAULT 0,
    progress_message TEXT,
    data_version TEXT,
    code_version TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    started_at TEXT,
    completed_at TEXT,
    source_experiment_id INTEGER
);

CREATE TABLE IF NOT EXISTS parameter_presets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    params TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'batch',
    pool_preset TEXT,
    pool_custom_codes TEXT,
    pool_industries TEXT,
    source_experiment_id INTEGER,
    metrics_snapshot TEXT,
    notes TEXT,
    labels TEXT,
    is_default INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    UNIQUE(user_id, strategy_id, name)
);

CREATE INDEX IF NOT EXISTS idx_parameter_presets_user_strategy
ON parameter_presets(user_id, strategy_id, updated_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS idx_parameter_presets_one_default
ON parameter_presets(user_id, strategy_id)
WHERE is_default = 1;

CREATE TABLE IF NOT EXISTS ai_cache (
    cache_key TEXT PRIMARY KEY,
    result_json TEXT NOT NULL,
    expires_at REAL NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_ai_cache_expires ON ai_cache(expires_at);

CREATE TABLE IF NOT EXISTS ai_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint TEXT NOT NULL,
    user_id INTEGER,
    cache_key TEXT NOT NULL,
    model TEXT,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    latency_ms REAL NOT NULL DEFAULT 0,
    cache_hit INTEGER NOT NULL DEFAULT 0,
    success INTEGER NOT NULL DEFAULT 1,
    error_type TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_ai_usage_user_created
ON ai_usage(user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_ai_usage_endpoint_created
ON ai_usage(endpoint, created_at DESC);

CREATE TABLE IF NOT EXISTS experiment_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id INTEGER NOT NULL UNIQUE,
    cumulative_return REAL,
    sharpe_ratio REAL,
    annual_return REAL,
    max_drawdown REAL,
    volatility REAL,
    calmar_ratio REAL,
    sortino_ratio REAL,
    win_rate REAL,
    profit_loss_ratio REAL,
    avg_trade_return REAL,
    max_consecutive_wins INTEGER,
    max_consecutive_losses INTEGER,
    total_trades INTEGER,
    avg_holding_days REAL,
    turnover_rate REAL,
    information_ratio REAL,
    treynor_ratio REAL,
    alpha REAL,
    beta REAL,
    tracking_error REAL,
    upside_capture REAL,
    downside_capture REAL,
    var_95 REAL,
    cvar_95 REAL,
    skewness REAL,
    kurtosis REAL,
    daily_sharpe REAL,
    monthly_sharpe REAL,
    yearly_return REAL,
    recovery_days INTEGER,
    max_drawdown_duration INTEGER,
    avg_drawdown REAL,
    avg_drawdown_days REAL,
    best_month REAL,
    worst_month REAL,
    positive_months REAL,
    profit_factor REAL,
    expectency REAL,
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (experiment_id) REFERENCES experiments(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS equity_curve (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    equity REAL NOT NULL,
    benchmark REAL,
    daily_return REAL,
    drawdown REAL,
    FOREIGN KEY (experiment_id) REFERENCES experiments(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_equity_exp_date ON equity_curve(experiment_id, date);

CREATE TABLE IF NOT EXISTS trade_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    signal_date TEXT,
    code TEXT NOT NULL,
    action TEXT NOT NULL,
    price REAL NOT NULL,
    shares INTEGER NOT NULL,
    amount REAL NOT NULL,
    cost REAL NOT NULL,
    signal_strategy TEXT DEFAULT '',
    signal_score REAL DEFAULT 0.0,
    FOREIGN KEY (experiment_id) REFERENCES experiments(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_trade_exp ON trade_log(experiment_id);

CREATE TABLE IF NOT EXISTS model_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id INTEGER NOT NULL,
    strategy_id TEXT NOT NULL,
    model_version INTEGER DEFAULT 1,
    model_file_path TEXT NOT NULL,
    metadata_file_path TEXT NOT NULL,
    params_hash TEXT NOT NULL,
    train_window_start TEXT,
    train_window_end TEXT,
    feature_count INTEGER,
    train_samples INTEGER,
    train_metrics TEXT,
    feature_importance TEXT,
    artifact_sha256 TEXT,
    artifact_size INTEGER,
    run_manifest_hash TEXT,
    is_latest INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (experiment_id) REFERENCES experiments(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS param_sweeps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    strategy_id TEXT NOT NULL,
    name TEXT,
    sweep_config TEXT NOT NULL,
    total_experiments INTEGER DEFAULT 0,
    completed_experiments INTEGER DEFAULT 0,
    status TEXT DEFAULT 'pending',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sweep_experiments (
    sweep_id INTEGER NOT NULL,
    experiment_id INTEGER NOT NULL,
    param_combo TEXT NOT NULL,
    PRIMARY KEY (sweep_id, experiment_id),
    FOREIGN KEY (sweep_id) REFERENCES param_sweeps(id) ON DELETE CASCADE,
    FOREIGN KEY (experiment_id) REFERENCES experiments(id) ON DELETE CASCADE
);
"""

_TRADING_SIM_SCHEMA = """
CREATE TABLE IF NOT EXISTS deployments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    strategy_id TEXT NOT NULL,
    strategy_category TEXT NOT NULL,
    display_name TEXT,
    params TEXT NOT NULL,
    params_hash TEXT NOT NULL,
    mode TEXT DEFAULT 'batch',
    source_experiment_id INTEGER,
    source_model_artifact_id INTEGER,
    research_promotion_id INTEGER,
    promotion_version INTEGER,
    promotion_report_id INTEGER,
    promotion_report_hash TEXT,
    promotion_manifest_hash TEXT,
    promotion_model_artifact_id INTEGER,
    promotion_model_sha256 TEXT,
    promotion_evidence_hash TEXT,
    promotion_binding_hash TEXT,
    research_risk_snapshot TEXT,
    research_risk_snapshot_hash TEXT,
    research_generation_id TEXT,
    research_source_id TEXT,
    research_window_start TEXT,
    research_window_end TEXT,
    requires_retraining INTEGER DEFAULT 0,
    retrain_frequency TEXT,
    last_retrain_at TEXT,
    current_model_version INTEGER DEFAULT 1,
    current_model_path TEXT,
    current_model_sha256 TEXT,
    current_model_size INTEGER,
    position_mode TEXT DEFAULT 'equal_weight',
    position_config TEXT,
    status TEXT DEFAULT 'active',
    status_tags TEXT,
    user_notes TEXT,
    deployed_at TEXT DEFAULT (datetime('now')),
    last_signal_at TEXT,
    last_rebalance_at TEXT,
    stopped_at TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS portfolios (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    total_capital REAL NOT NULL,
    rebalance_frequency TEXT DEFAULT 'monthly',
    allocations TEXT,
    status TEXT DEFAULT 'active',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS daily_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deployment_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    code TEXT NOT NULL,
    action TEXT NOT NULL,
    score REAL DEFAULT 0.0,
    weight REAL DEFAULT 0.0,
    confidence REAL DEFAULT 0.0,
    reasoning TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (deployment_id) REFERENCES deployments(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_signals_deploy_date ON daily_signals(deployment_id, date);

CREATE TABLE IF NOT EXISTS position_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id INTEGER,
    deployment_id INTEGER,
    date TEXT NOT NULL,
    code TEXT NOT NULL,
    shares INTEGER NOT NULL,
    avg_cost REAL NOT NULL,
    close_price REAL NOT NULL,
    market_value REAL NOT NULL,
    unrealized_pnl REAL NOT NULL,
    FOREIGN KEY (portfolio_id) REFERENCES portfolios(id) ON DELETE SET NULL,
    FOREIGN KEY (deployment_id) REFERENCES deployments(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_positions_date ON position_snapshots(date);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deployment_id INTEGER NOT NULL,
    portfolio_id INTEGER,
    date TEXT NOT NULL,
    code TEXT NOT NULL,
    action TEXT NOT NULL,
    price REAL NOT NULL,
    shares INTEGER NOT NULL,
    amount REAL NOT NULL,
    cost REAL NOT NULL,
    signal_strategy TEXT DEFAULT '',
    signal_score REAL DEFAULT 0.0,
    order_type TEXT DEFAULT 'market',
    status TEXT DEFAULT 'pending',
    reject_reason TEXT DEFAULT '',
    order_intent_id TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (deployment_id) REFERENCES deployments(id) ON DELETE CASCADE,
    FOREIGN KEY (portfolio_id) REFERENCES portfolios(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_orders_deploy_date ON orders(deployment_id, date);
CREATE UNIQUE INDEX IF NOT EXISTS uq_orders_intent
ON orders(order_intent_id) WHERE order_intent_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS model_version_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deployment_id INTEGER NOT NULL,
    model_version INTEGER NOT NULL,
    model_file_path TEXT NOT NULL,
    metadata_file_path TEXT NOT NULL,
    train_metrics TEXT,
    feature_importance TEXT,
    train_window_start TEXT,
    train_window_end TEXT,
    validation_window_start TEXT,
    validation_window_end TEXT,
    validation_metrics TEXT,
    model_sha256 TEXT,
    model_size INTEGER,
    strategy_id TEXT,
    params_hash TEXT,
    retrain_manifest_json TEXT,
    retrain_manifest_hash TEXT,
    status TEXT NOT NULL DEFAULT 'promoted',
    error TEXT,
    is_latest INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(deployment_id, model_version),
    FOREIGN KEY (deployment_id) REFERENCES deployments(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS model_retrain_attempts (
    attempt_id TEXT PRIMARY KEY,
    deployment_id INTEGER NOT NULL,
    expected_model_version INTEGER NOT NULL,
    candidate_model_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    train_window_start TEXT,
    train_window_end TEXT,
    validation_window_start TEXT,
    validation_window_end TEXT,
    validation_metrics TEXT,
    model_sha256 TEXT,
    model_size INTEGER,
    retrain_manifest_json TEXT,
    retrain_manifest_hash TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    FOREIGN KEY (deployment_id) REFERENCES deployments(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS nav_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id INTEGER,
    deployment_id INTEGER,
    date TEXT NOT NULL,
    nav REAL NOT NULL,
    daily_return REAL,
    cumulative_return REAL,
    FOREIGN KEY (portfolio_id) REFERENCES portfolios(id) ON DELETE SET NULL,
    FOREIGN KEY (deployment_id) REFERENCES deployments(id) ON DELETE SET NULL
);
"""


async def _init_databases() -> None:
    """初始化所有数据库，执行迁移 SQL。"""
    import aiosqlite

    db_files = [
        (settings.abs_path(settings.USERS_DB), _USERS_SCHEMA),
        (settings.abs_path(settings.EXPERIMENT_DB), _EXPERIMENT_SCHEMA),
        (settings.abs_path(settings.TRADING_SIM_DB), _TRADING_SIM_SCHEMA),
    ]

    for db_path, schema_sql in db_files:
        db_path.parent.mkdir(parents=True, exist_ok=True)

        async with aiosqlite.connect(str(db_path)) as conn:
            await conn.executescript(schema_sql)
            if db_path == settings.abs_path(settings.USERS_DB):
                from backend.auth.sessions import ensure_auth_session_schema
                await ensure_auth_session_schema(conn)
            elif db_path == settings.abs_path(settings.EXPERIMENT_DB):
                from backend.db.migrate import migrate_experiment
                await migrate_experiment(conn)
            elif db_path == settings.abs_path(settings.TRADING_SIM_DB):
                from backend.db.migrate import migrate_trading
                await migrate_trading(conn)
            await conn.commit()

        logger.info("Database initialized: %s", db_path)


async def _scan_strategies() -> None:
    """启动时扫描策略目录，注册所有策略。"""
    try:
        from backend.strategies.registry import get_registry
        registry = get_registry()
        strategies_dir = settings.PROJECT_ROOT / "backend" / "strategies"
        registry.scan_directory(strategies_dir)
        from backend.strategies.factor._configured_factor import (
            load_factor_strategy_definitions,
            make_factor_strategy_class,
        )
        for definition in load_factor_strategy_definitions():
            try:
                registry.register_strategy_class(
                    make_factor_strategy_class(definition)
                )
            except ValueError:
                logger.exception(
                    "Failed to register exported factor strategy %s",
                    definition.get("strategy_id"),
                )
        from backend.data.factor_governance import FactorGovernanceStore
        for definition in (
            FactorGovernanceStore().list_active_strategy_definitions()
        ):
            try:
                registry.replace_strategy_class(
                    make_factor_strategy_class(definition)
                )
            except ValueError:
                logger.exception(
                    "Failed to register governed factor strategy %s",
                    definition.get("strategy_id"),
                )
        logger.info(
            "Strategies scanned: %d loaded", len(registry.list_all())
        )
    except Exception:
        logger.exception("Strategy scan failed — continuing without strategies")


def _parse_list(value: object) -> list[str]:
    """Parse a JSON array or comma-separated database field."""
    import json

    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
    except json.JSONDecodeError:
        pass
    return [item.strip() for item in text.replace("，", ",").split(",") if item.strip()]


def _filter_pivot_codes(pivot, codes: list[str]):
    """Filter either a simple or MultiIndex price pivot by security code."""
    import pandas as pd

    if not codes:
        return pivot
    wanted = set(codes)
    if isinstance(pivot.columns, pd.MultiIndex):
        keep = [column for column in pivot.columns if str(column[0]) in wanted]
    else:
        keep = [column for column in pivot.columns if str(column) in wanted]
    return pivot.loc[:, keep]


def _safe_metric(metrics: dict, *keys: str):
    """Return a finite metric or None so unavailable values stay SQL NULL."""
    import math

    for key in keys:
        value = metrics.get(key)
        if isinstance(value, (int, float)) and math.isfinite(float(value)):
            return value
    return None


async def _refresh_related_sweep_status(db, experiment_id: int) -> None:
    """Recompute terminal progress for every sweep containing an experiment."""

    await db.execute(
        """
        UPDATE param_sweeps
        SET completed_experiments = (
                SELECT COUNT(*) FROM sweep_experiments se
                JOIN experiments e ON e.id = se.experiment_id
                WHERE se.sweep_id = param_sweeps.id
                  AND e.status IN ('completed', 'failed', 'cancelled')
            ),
            status = CASE
                WHEN total_experiments <= (
                    SELECT COUNT(*) FROM sweep_experiments se
                    JOIN experiments e ON e.id = se.experiment_id
                    WHERE se.sweep_id = param_sweeps.id
                      AND e.status IN ('completed', 'failed', 'cancelled')
                ) THEN 'completed'
                ELSE 'running'
            END
        WHERE id IN (
            SELECT sweep_id FROM sweep_experiments WHERE experiment_id = ?
        )
        """,
        (experiment_id,),
    )


async def _run_experiment(exp_id: int, job_uuid: str) -> None:
    """Execute and atomically persist one reproducible backtest run."""
    import hashlib
    import json

    import aiosqlite
    import pandas as pd

    from backend.jobs.broker import JobCancelledError, get_broker

    broker = get_broker()
    db_path = str(settings.abs_path(settings.EXPERIMENT_DB))

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM experiments WHERE id = ?", (exp_id,))
        exp = await cursor.fetchone()
        if exp is None:
            raise ValueError(f"实验不存在: {exp_id}")
        if exp["status"] == "completed":
            cursor = await db.execute(
                "SELECT COUNT(*) FROM equity_curve WHERE experiment_id = ?",
                (exp_id,),
            )
            if (await cursor.fetchone())[0] > 0:
                from backend.services.research_manifest import load_run_manifest

                # Duplicate queue delivery is a no-op, but manifest tampering
                # must still fail closed instead of being silently accepted.
                await load_run_manifest(db_path, exp_id)
                await broker.update_job_progress(job_uuid, progress=1.0, status="completed")
                return
        await db.execute(
            """
            UPDATE experiments
            SET status='running', error_log=NULL, progress_pct=5,
                progress_message='加载数据', started_at=COALESCE(started_at, datetime('now'))
            WHERE id=?
            """,
            (exp_id,),
        )
        await db.commit()

    try:
        await broker.update_job_progress(
            job_uuid,
            progress=0.05,
            status="running",
            message="加载本地行情数据",
            stage="loading_data",
        )
        await broker.raise_if_cancelled(job_uuid)
        raw_pool = exp["pool_preset"] or "csi300"
        from backend.data.cache import DataCache, resolve_pool_benchmark
        from backend.data.cache_readiness import (
            custom_cache_key,
            require_cached_benchmark,
            require_cached_market_data,
        )
        from backend.data.lineage import (
            STATIC_UNIVERSE,
            UniverseSnapshot,
            build_universe_snapshot,
        )
        from backend.data.research_snapshots import (
            ResearchSnapshotStore,
            clip_to_test_end,
        )
        from backend.data.universe import POOL_NAME_ALIASES, UniverseManager
        from backend.data.versioning import compute_dataset_version

        pool_id = POOL_NAME_ALIASES.get(raw_pool, raw_pool)
        run_spec: dict = {}
        if exp["run_spec"]:
            try:
                parsed_run_spec = json.loads(exp["run_spec"])
                if isinstance(parsed_run_spec, dict):
                    run_spec = parsed_run_spec
                else:
                    raise ValueError("实验 run_spec 必须是 JSON object")
            except json.JSONDecodeError:
                raise ValueError("实验 run_spec JSON 已损坏") from None
        replay_spec = run_spec.get("research_replay", {})
        if not isinstance(replay_spec, dict):
            replay_spec = {}
        data_access_policy = run_spec.get(
            "data_access_policy",
            "allow_fetch",
        )
        if data_access_policy not in {"allow_fetch", "cache_only"}:
            raise ValueError(
                "实验数据访问策略无效；仅支持 allow_fetch 或 cache_only"
            )
        research_trust_profile = str(
            run_spec.get("research_trust_profile") or "governed_production_pit"
        )
        research_trust = run_spec.get("research_trust")
        if research_trust_profile == "tushare_research_trusted":
            if not isinstance(research_trust, dict):
                raise ValueError("Tushare 条件信任实验缺少持久化证据")
        elif research_trust_profile != "governed_production_pit":
            raise ValueError("实验研究信任档案无效")
        replay_dataset_snapshot = replay_spec.get("dataset_snapshot")
        replay_execution_spec = replay_spec.get("execution")
        if not isinstance(replay_execution_spec, dict):
            replay_execution_spec = {}
        uses_immutable_snapshot = isinstance(replay_dataset_snapshot, dict)
        calculation_anchor = pd.Timestamp(
            exp["train_start"] or exp["test_start"]
        )
        calculation_start = (
            calculation_anchor - pd.Timedelta(days=400)
        ).strftime("%Y-%m-%d")
        preliminary_timeline = None

        # Formal execution is PIT-only.  The worker repeats this gate even
        # when submission already performed a preflight: queued jobs can
        # outlive a cache replacement, governance decision or code deploy.
        from backend.data.pit_runtime import (
            PitRuntimeDataError,
            require_pit_pool,
            require_pit_runtime_input,
        )

        pool_id = require_pit_pool(pool_id)
        if data_access_policy != "cache_only":
            raise PitRuntimeDataError(
                "pit_cache_only_required",
                "PIT-only 模式禁止实验在运行时联网补数；请使用本地已激活治理数据",
            )
        if uses_immutable_snapshot:
            replay_universe_evidence = replay_spec.get("universe")
            replay_execution_evidence = replay_spec.get("execution")
            if not (
                isinstance(replay_universe_evidence, dict)
                and replay_universe_evidence.get("point_in_time") is True
                and isinstance(
                    replay_universe_evidence.get("timeline_identity"),
                    dict,
                )
                and isinstance(replay_execution_evidence, dict)
                and isinstance(
                    replay_execution_evidence.get("canonical_price_binding"),
                    dict,
                )
                and replay_spec.get("pit_only_runtime_verified") is True
            ):
                raise PitRuntimeDataError(
                    "pit_replay_evidence_missing",
                    "精确重跑来源未绑定 PIT-only 运行证明，禁止重放旧静态快照",
                )
        elif research_trust_profile == "tushare_research_trusted":
            # Submission binds an immutable ResearchDataStore generation. The
            # worker loads that exact generation below; it must not rebuild a
            # separate legacy evidence timeline or drift to the active pointer.
            pass
        else:
            await require_pit_runtime_input(
                pool_id=pool_id,
                required_start=calculation_start,
                required_end=exp["test_end"],
                purpose="research",
            )

        cache = DataCache()
        source = None
        if data_access_policy == "allow_fetch":
            from backend.data.sources.validated import (
                build_public_research_source,
            )

            source = build_public_research_source()
        universe = UniverseManager(source, cache)
        selected_codes = _parse_list(exp["pool_custom_codes"])
        industries = _parse_list(exp["pool_industries"])
        runtime_cache_key: str | None = None
        cache_source_provenance: dict[str, object] | None = None
        raw_execution_pivot = None
        runtime_price_binding: dict[str, object] | None = None
        qa_runtime_attestation: dict[str, object] | None = None
        research_market_result: dict[str, object] | None = None
        research_benchmark_report: dict[str, object] | None = None
        snapshot_store = ResearchSnapshotStore(
            settings.abs_path(settings.RESEARCH_SNAPSHOT_DIR)
        )
        universe_snapshot = None
        point_in_time_timeline = None
        replay_benchmark = replay_spec.get("benchmark")
        if (
            not uses_immutable_snapshot
            and pool_id in {"csi300", "csi500", "csi800", "csi1000"}
            and research_trust_profile != "tushare_research_trusted"
        ):
            from backend.data.point_in_time_master import (
                PointInTimeMasterStore,
            )
            from backend.data.point_in_time_universe import (
                resolve_point_in_time_universe,
            )
            from backend.data.universe import PRESET_POOLS

            pit_store = PointInTimeMasterStore()
            # Weekdays are used only to discover the union before a market
            # cache exists. The execution identity is resolved again from the
            # exact observed sessions after data loading.
            preliminary_timeline = resolve_point_in_time_universe(
                pit_store,
                pool_id=pool_id,
                trading_dates=pd.bdate_range(
                    calculation_start,
                    exp["test_end"],
                ),
                expected_count=PRESET_POOLS[pool_id]["expected_count"],
            )
        if uses_immutable_snapshot:
            pivot = snapshot_store.load_pivot(replay_dataset_snapshot)
            replay_raw_execution_snapshot = replay_execution_spec.get(
                "raw_execution_snapshot"
            )
            if isinstance(replay_raw_execution_snapshot, dict):
                raw_execution_pivot = snapshot_store.load_pivot(
                    replay_raw_execution_snapshot
                )
                runtime_price_binding = replay_execution_spec.get(
                    "canonical_price_binding"
                )
            replay_universe = replay_spec.get("universe")
            if not isinstance(replay_universe, dict):
                raise ValueError("精确重跑缺少不可变股票池快照")
            universe_snapshot = UniverseSnapshot.from_dict(replay_universe)
            if universe_snapshot.pool_id != pool_id:
                raise ValueError("精确重跑股票池标识与来源快照不一致")
            if not isinstance(replay_benchmark, dict):
                raise ValueError("精确重跑缺少基准快照证据")
            benchmark_code = str(replay_benchmark.get("code") or "")
            benchmark_fetch_start = str(
                replay_benchmark.get("fetch_start") or ""
            )
            if replay_benchmark.get("available"):
                benchmark_snapshot = replay_benchmark.get("snapshot")
                if not isinstance(benchmark_snapshot, dict):
                    raise ValueError("精确重跑缺少可用基准的数据快照")
                benchmark_close = snapshot_store.load_benchmark(
                    benchmark_snapshot
                )
            else:
                benchmark_close = pd.Series(dtype="float64", name="close")
        else:
            if pool_id == "custom":
                if not selected_codes:
                    raise ValueError("自定义股票池必须提供股票代码")
                custom_key = custom_cache_key(selected_codes)
                runtime_cache_key = custom_key
                required_start = exp["train_start"] or (
                    exp["test_start"]
                    if data_access_policy == "cache_only"
                    else "2015-01-01"
                )
                required_end = exp["test_end"]
                if data_access_policy == "cache_only":
                    inspected = await require_cached_market_data(
                        cache,
                        cache_key=custom_key,
                        pool_id=pool_id,
                        requested_codes=selected_codes,
                        required_start=required_start,
                        required_end=required_end,
                    )
                    assert inspected.frame is not None
                    pivot = inspected.frame
                    cache_source_provenance = (
                        inspected.source_provenance
                    )
                else:
                    from backend.data.cache import has_price_field

                    pivot = await cache.load_pivot(custom_key)
                    cache_covers_window = (
                        pivot is not None
                        and not pivot.empty
                        and has_price_field(pivot, "open")
                        and pivot.index.min() <= pd.Timestamp(required_start)
                        and pivot.index.max() >= pd.Timestamp(required_end)
                    )
                    if not cache_covers_window:
                        assert source is not None
                        pivot = await cache.get_or_fetch_custom(
                            custom_key,
                            source,
                            selected_codes,
                            required_start,
                            required_end,
                            force=True,
                        )
            elif (
                data_access_policy == "cache_only"
                and research_trust_profile == "tushare_research_trusted"
            ):
                from backend.services.research_runtime import (
                    load_research_market,
                    normalize_research_provenance,
                    verify_research_runtime_binding,
                )

                runtime_binding = research_trust.get("runtime_binding") or {}
                bound_generation_id = runtime_binding.get("generation_id")
                if not isinstance(bound_generation_id, str):
                    raise ValueError("Tushare 研究实验缺少数据代绑定")
                research_market_result = await load_research_market(
                    pool_id=pool_id,
                    required_start=calculation_start,
                    required_end=exp["test_end"],
                    generation_id=bound_generation_id,
                )
                pivot = research_market_result["frame"]
                market_report = research_market_result["report"]
                existing_evidence = research_trust.get("evidence") or {}
                if not market_report.get("candidate_report_sha256"):
                    market_report["candidate_report_sha256"] = (
                        existing_evidence.get("candidate_report_sha256")
                    )
                runtime_cache_key = (
                    f"research:{bound_generation_id}:{pool_id}"
                )
                cache_source_provenance = normalize_research_provenance(
                    research_market_result["source_provenance"]
                )
                verify_research_runtime_binding(
                    runtime_binding,
                    research_market_result,
                )
                research_trust.setdefault("warnings", []).extend(
                    market_report.get("warnings") or []
                )
                # The store contains raw columns for audit/reconstruction, but
                # paper fills remain adjusted-price compatibility until the
                # corporate-action state machine consumes a certified ledger.
                research_trust.setdefault("warnings", []).append(
                    "production_dual_price_ledger_not_certified"
                )
            elif data_access_policy == "cache_only":
                runtime_cache_key = pool_id
                inspected = await require_cached_market_data(
                    cache,
                    cache_key=pool_id,
                    pool_id=pool_id,
                    requested_codes=(
                        preliminary_timeline.union_codes
                        if preliminary_timeline is not None
                        else selected_codes
                    ),
                    required_start=calculation_start,
                    required_end=exp["test_end"],
                )
                assert inspected.frame is not None
                pivot = inspected.frame
                cache_source_provenance = inspected.source_provenance
                raw_execution_pivot = inspected.raw_execution_frame
                runtime_price_binding = inspected.runtime_price_binding
                raw_qa_attestation = inspected.report.get(
                    "qa_runtime_attestation"
                )
                if isinstance(raw_qa_attestation, dict):
                    qa_runtime_attestation = dict(raw_qa_attestation)
                if research_trust_profile == "tushare_research_trusted":
                    # Conditional research intentionally does not borrow a
                    # production dual-price claim from an unrelated binding.
                    raw_execution_pivot = None
                    runtime_price_binding = None
                    qa_runtime_attestation = None
            else:
                assert source is not None
                runtime_cache_key = pool_id
                if preliminary_timeline is not None:
                    pivot = await cache.get_or_fetch_point_in_time_universe(
                        pool_id,
                        source,
                        list(preliminary_timeline.union_codes),
                        calculation_start,
                        exp["test_end"],
                    )
                else:
                    # Non-index compatibility pools retain their explicit
                    # static-universe risk until a PIT resolver exists.
                    pivot = await cache.get_or_fetch(
                        pool_id,
                        source,
                        start=calculation_start,
                        end=exp["test_end"],
                    )
        if pivot is None or pivot.empty:
            raise FileNotFoundError(f"股票池 {pool_id} 没有可用行情数据")
        if not uses_immutable_snapshot:
            if runtime_cache_key is None:
                raise ValueError("运行缓存身份缺失")
            if cache_source_provenance is None:
                cache_source_provenance = cache.get_source_provenance(
                    runtime_cache_key
                )
        if not isinstance(pivot.index, pd.DatetimeIndex):
            pivot.index = pd.to_datetime(pivot.index)
        # The immutable research input ends at test_end. Future rows from a
        # cache extension must not affect hashes, factors, or strategy inputs.
        pivot = clip_to_test_end(pivot, exp["test_end"])
        if raw_execution_pivot is not None:
            raw_execution_pivot = clip_to_test_end(
                raw_execution_pivot,
                exp["test_end"],
            )
        if not uses_immutable_snapshot:
            pivot = pivot.loc[pivot.index >= pd.Timestamp(calculation_start)]
            if raw_execution_pivot is not None:
                raw_execution_pivot = raw_execution_pivot.loc[
                    raw_execution_pivot.index
                    >= pd.Timestamp(calculation_start)
                ]
        if pivot.empty:
            raise ValueError("测试结束日前没有可用行情数据")

        if (
            not uses_immutable_snapshot
            and pool_id in {"csi300", "csi500", "csi800", "csi1000"}
        ):
            from backend.data.point_in_time_master import (
                PointInTimeMasterStore,
            )
            from backend.data.point_in_time_universe import (
                filter_timeline_by_industry,
                filter_timeline_codes,
                resolve_point_in_time_universe,
                select_market_data_for_timeline,
            )
            from backend.data.universe import PRESET_POOLS

            pit_store = PointInTimeMasterStore()
            if research_trust_profile == "tushare_research_trusted":
                from backend.data.point_in_time_universe import (
                    timeline_from_identity,
                )

                timeline_identity = (
                    research_market_result.get("report", {}).get(
                        "timeline_identity"
                    )
                    if research_market_result is not None
                    else None
                )
                if not isinstance(timeline_identity, dict):
                    raise ValueError("研究数据代缺少可重放的 PIT 股票池时间线")
                point_in_time_timeline = timeline_from_identity(
                    timeline_identity,
                    trading_dates=pivot.index,
                )
            else:
                point_in_time_timeline = resolve_point_in_time_universe(
                    pit_store,
                    pool_id=pool_id,
                    trading_dates=pivot.index,
                    expected_count=PRESET_POOLS[pool_id]["expected_count"],
                )
            point_in_time_timeline = filter_timeline_codes(
                point_in_time_timeline,
                selected_codes,
            )
            if industries and research_trust_profile != "tushare_research_trusted":
                point_in_time_timeline = filter_timeline_by_industry(
                    pit_store,
                    point_in_time_timeline,
                    industries,
                )
            pivot = select_market_data_for_timeline(
                pivot,
                point_in_time_timeline,
            )
            if raw_execution_pivot is not None:
                raw_execution_pivot = select_market_data_for_timeline(
                    raw_execution_pivot,
                    point_in_time_timeline,
                )
        elif (
            not uses_immutable_snapshot
            and research_trust_profile == "tushare_research_trusted"
        ):
            from backend.data.point_in_time_universe import (
                filter_timeline_codes,
                select_market_data_for_timeline,
                timeline_from_identity,
            )

            timeline_identity = (
                research_market_result.get("report", {}).get(
                    "timeline_identity"
                )
                if research_market_result is not None
                else None
            )
            if not isinstance(timeline_identity, dict):
                raise ValueError("研究数据代缺少可重放的 PIT 股票池时间线")
            point_in_time_timeline = timeline_from_identity(
                timeline_identity,
                trading_dates=pivot.index,
            )
            point_in_time_timeline = filter_timeline_codes(
                point_in_time_timeline,
                selected_codes,
            )
            pivot = select_market_data_for_timeline(
                pivot,
                point_in_time_timeline,
            )
        elif uses_immutable_snapshot and universe_snapshot is not None:
            timeline_identity = universe_snapshot.timeline_identity
            if timeline_identity is not None:
                from backend.data.point_in_time_universe import (
                    select_market_data_for_timeline,
                    timeline_from_identity,
                )

                point_in_time_timeline = timeline_from_identity(
                    timeline_identity,
                    trading_dates=pivot.index,
                )
                pivot = select_market_data_for_timeline(
                    pivot,
                    point_in_time_timeline,
                )
        if industries and not uses_immutable_snapshot and (
            point_in_time_timeline is None
            or research_trust_profile == "tushare_research_trusted"
        ):
            available_codes = (
                [str(column[0]) for column in pivot.columns]
                if isinstance(pivot.columns, pd.MultiIndex)
                else [str(column) for column in pivot.columns]
            )
            candidate_codes = (
                sorted(set(available_codes).intersection(selected_codes))
                if selected_codes
                else available_codes
            )
            # Coverage must describe the exact securities that will be used.
            # Requiring a full preset-pool mapping after the user chose a
            # smaller stock subset makes the UI readiness evidence invalid.
            selected_codes = await universe.filter_by_industry(
                candidate_codes,
                industries,
            )
        pivot = _filter_pivot_codes(pivot, selected_codes)
        if raw_execution_pivot is not None:
            raw_execution_pivot = _filter_pivot_codes(
                raw_execution_pivot,
                selected_codes,
            )
        if pivot.empty or len(pivot.columns) == 0:
            raise ValueError("自定义股票池或行业筛选后没有可用标的")

        if not uses_immutable_snapshot:
            benchmark_code = resolve_pool_benchmark(pool_id)
            benchmark_fetch_start = (
                pd.Timestamp(exp["test_start"]) - pd.Timedelta(days=10)
            ).strftime("%Y-%m-%d")
            if data_access_policy == "cache_only":
                if research_trust_profile == "tushare_research_trusted":
                    from backend.services.research_runtime import (
                        build_research_trust,
                        load_research_benchmark,
                    )

                    runtime_binding = research_trust.get("runtime_binding") or {}
                    bound_generation_id = str(
                        runtime_binding.get("generation_id") or ""
                    )
                    benchmark_result = await load_research_benchmark(
                        index_code=benchmark_code,
                        required_start=benchmark_fetch_start,
                        required_end=exp["test_end"],
                        generation_id=bound_generation_id,
                    )
                    research_benchmark_report = dict(
                        benchmark_result.get("report") or {}
                    )
                    benchmark_close = (
                        benchmark_result.get("series")
                        if benchmark_result.get("series") is not None
                        else pd.Series(dtype="float64", name="close")
                    )
                    if research_market_result is None:
                        raise ValueError("研究数据运行时结果缺失")
                    refreshed_trust = build_research_trust(
                        market_result=research_market_result,
                        required_start=calculation_start,
                        required_end=exp["test_end"],
                        purpose=str(research_trust.get("purpose") or "return_research"),
                        benchmark_report=research_benchmark_report,
                    )
                    refreshed_trust.setdefault("warnings", []).extend(
                        research_trust.get("warnings") or []
                    )
                    research_trust = refreshed_trust
                else:
                    inspected_benchmark = await require_cached_benchmark(
                        cache,
                        index_code=benchmark_code,
                        required_start=benchmark_fetch_start,
                        required_end=exp["test_end"],
                    )
                    assert inspected_benchmark.series is not None
                    benchmark_close = inspected_benchmark.series
            else:
                assert source is not None
                try:
                    benchmark_close = await cache.get_or_fetch_index(
                        benchmark_code,
                        source,
                        start=benchmark_fetch_start,
                        end=exp["test_end"],
                    )
                except Exception:
                    logger.exception(
                        "基准指数 %s 加载失败；实验将继续，但不计算相对指标",
                        benchmark_code,
                    )
                    benchmark_close = pd.Series(
                        dtype="float64",
                        name="close",
                    )
        if not benchmark_close.empty:
            if not isinstance(benchmark_close.index, pd.DatetimeIndex):
                benchmark_close.index = pd.to_datetime(benchmark_close.index)
            benchmark_close = clip_to_test_end(
                benchmark_close,
                exp["test_end"],
            )

        actual_codes = sorted(
            {
                str(column[0] if isinstance(column, tuple) else column)
                for column in pivot.columns
            }
        )
        if uses_immutable_snapshot:
            assert universe_snapshot is not None
            if tuple(actual_codes) != universe_snapshot.codes:
                raise ValueError("精确重跑数据列与不可变股票池不一致")
        elif point_in_time_timeline is not None:
            universe_snapshot = build_universe_snapshot(
                pool_id,
                actual_codes,
                requested_as_of=exp["test_start"],
                source_as_of=exp["test_start"],
                point_in_time=True,
                # ``actual_codes`` is the union across the full timeline and
                # is intentionally larger than a fixed daily index size.
                expected_count=None,
                timeline_identity=point_in_time_timeline.identity(),
            )
        elif pool_id == "custom" or data_access_policy == "cache_only":
            expected_count = None
            if pool_id != "custom":
                from backend.data.universe import PRESET_POOLS

                expected_count = PRESET_POOLS.get(pool_id, {}).get(
                    "expected_count"
                )
            universe_snapshot = build_universe_snapshot(
                pool_id,
                actual_codes,
                requested_as_of=exp["test_start"],
                source_as_of=None,
                point_in_time=False,
                expected_count=expected_count,
                risk_warnings=(STATIC_UNIVERSE,),
            )
        else:
            source_universe = await universe.get_pool_snapshot(
                pool_id,
                exp["test_start"],
                include_industry_quality=False,
            )
            universe_snapshot = build_universe_snapshot(
                pool_id,
                actual_codes,
                requested_as_of=exp["test_start"],
                source_as_of=source_universe.source_as_of,
                point_in_time=source_universe.point_in_time,
                expected_count=source_universe.quality.expected_count,
                risk_warnings=source_universe.risk_warnings,
            )
        assert universe_snapshot is not None
        from backend.data.market_quality import audit_market_data

        quality_source = ""
        quality_adjustment = ""
        source_provenance_sha256: str | None = None
        replay_quality = replay_spec.get("market_data_quality")
        if uses_immutable_snapshot and isinstance(replay_quality, dict):
            source_metadata = replay_quality.get("source")
            if isinstance(source_metadata, dict):
                quality_source = str(
                    source_metadata.get("provider") or ""
                )
                quality_adjustment = str(
                    source_metadata.get("price_adjustment")
                    or ""
                )
                replay_provenance_sha256 = source_metadata.get(
                    "provenance_sha256"
                )
                if isinstance(replay_provenance_sha256, str):
                    source_provenance_sha256 = replay_provenance_sha256
        elif cache_source_provenance is not None:
            providers = cache_source_provenance.get("providers")
            adjustments = cache_source_provenance.get("adjustments")
            if (
                not isinstance(providers, list)
                or len(providers) != 1
                or not isinstance(providers[0], str)
                or not providers[0]
                or not isinstance(adjustments, list)
                or len(adjustments) != 1
                or not isinstance(adjustments[0], str)
                or not adjustments[0]
            ):
                raise ValueError("缓存来源身份或复权口径不唯一")
            quality_source = providers[0]
            quality_adjustment = adjustments[0]
            provenance_digest = cache_source_provenance.get("content_sha256")
            if not isinstance(provenance_digest, str):
                raise ValueError("缓存来源证据缺少内容哈希")
            source_provenance_sha256 = provenance_digest
        if not quality_source or not quality_adjustment:
            raise ValueError("行情质量审计缺少已验证的来源身份或复权口径")
        market_data_quality = audit_market_data(
            pivot,
            test_end=exp["test_end"],
            source=quality_source,
            price_adjustment=quality_adjustment,
            source_provenance_sha256=source_provenance_sha256,
        )
        dataset_version = compute_dataset_version(
            pivot,
            context={
                "source": quality_source,
                "adjustment": quality_adjustment,
                "source_provenance_sha256": source_provenance_sha256,
                "pool_id": pool_id,
                "selected_codes": selected_codes,
                "industries": industries,
                "universe_snapshot_hash": universe_snapshot.snapshot_hash,
                "point_in_time_timeline_hash": (
                    point_in_time_timeline.timeline_hash
                    if point_in_time_timeline is not None
                    else None
                ),
            },
        )
        data_version = str(dataset_version)
        raw_execution_data_version = None
        if raw_execution_pivot is not None:
            raw_execution_data_version = compute_dataset_version(
                raw_execution_pivot,
                context={
                    "price_role": "raw_execution",
                    "pool_id": pool_id,
                    "point_in_time_timeline_hash": (
                        point_in_time_timeline.timeline_hash
                        if point_in_time_timeline is not None
                        else None
                    ),
                    "canonical_price_binding": runtime_price_binding,
                },
            )
            data_version = (
                f"{data_version}:raw_execution:"
                f"{raw_execution_data_version.digest[:16]}"
            )
        benchmark_content_hash = None
        if not benchmark_close.empty:
            benchmark_content_hash = hashlib.sha256(
                pd.util.hash_pandas_object(
                    benchmark_close,
                    index=True,
                ).values.tobytes()
            ).hexdigest()
            benchmark_fingerprint = (
                f"{benchmark_code}:{benchmark_content_hash}"
            )
            data_version = (
                f"{data_version}:benchmark:"
                f"{hashlib.sha256(benchmark_fingerprint.encode()).hexdigest()[:16]}"
            )
        if uses_immutable_snapshot:
            dataset_snapshot = replay_dataset_snapshot
            raw_execution_snapshot = replay_execution_spec.get(
                "raw_execution_snapshot"
            )
            benchmark_snapshot = (
                replay_benchmark.get("snapshot")
                if isinstance(replay_benchmark, dict)
                else None
            )
        else:
            dataset_snapshot = snapshot_store.save_pivot(pivot)
            raw_execution_snapshot = (
                snapshot_store.save_pivot(raw_execution_pivot)
                if raw_execution_pivot is not None
                else None
            )
            benchmark_snapshot = (
                snapshot_store.save_benchmark(benchmark_close)
                if not benchmark_close.empty
                else None
            )
        from backend.strategies.registry import get_registry

        registry = get_registry()
        strategy = registry.create_strategy(exp["strategy_id"])
        strategy_meta = registry.get_metadata(exp["strategy_id"])
        params = json.loads(exp["params"])
        from backend.strategies.base import split_platform_params

        strategy_params, _ = split_platform_params(params)
        is_valid, validation_error = strategy.validate_params(strategy_params)
        if not is_valid:
            raise ValueError(f"策略参数无效: {validation_error}")
        runtime_params = dict(strategy_params)
        if exp["train_start"]:
            runtime_params["_train_start"] = exp["train_start"]
        if exp["train_end"]:
            runtime_params["_train_end"] = exp["train_end"]

        from backend.services.research_manifest import (
            ManifestDriftError,
            build_run_manifest,
            expected_replay_differences,
            persist_initial_manifest,
            resolve_execution_payload,
        )

        benchmark_manifest = {
            "code": benchmark_code,
            "available": not benchmark_close.empty,
            "sha256": benchmark_content_hash,
            "fetch_start": benchmark_fetch_start,
            "fetch_end": exp["test_end"],
            **(
                {"snapshot": benchmark_snapshot}
                if benchmark_snapshot is not None
                else {}
            ),
        }
        execution_manifest = resolve_execution_payload(
            strategy,
            params,
            strategy_meta,
        )
        if qa_runtime_attestation is not None:
            execution_manifest["qa_runtime_attestation"] = (
                qa_runtime_attestation
            )
        if raw_execution_pivot is not None:
            applied_codes = sorted(
                {
                    str(item)
                    for item in pivot.columns.get_level_values(0)
                }
            )
            execution_manifest["raw_execution_snapshot"] = (
                raw_execution_snapshot
            )
            execution_manifest["raw_execution_data_version"] = str(
                raw_execution_data_version
            )
            execution_manifest["canonical_price_binding"] = (
                runtime_price_binding
            )
            execution_manifest["canonical_price_application"] = {
                "research_dataset_version": str(dataset_version),
                "raw_execution_dataset_version": str(
                    raw_execution_data_version
                ),
                "actual_start": pd.Timestamp(pivot.index.min()).strftime(
                    "%Y-%m-%d"
                ),
                "actual_end": pd.Timestamp(pivot.index.max()).strftime(
                    "%Y-%m-%d"
                ),
                "actual_code_count": len(applied_codes),
                "actual_codes_sha256": hashlib.sha256(
                    ",".join(applied_codes).encode("utf-8")
                ).hexdigest(),
                "semantics": (
                    "immutable_parent_binding_plus_hashed_runtime_subset"
                ),
            }
        if point_in_time_timeline is not None:
            execution_manifest["point_in_time_eligibility"] = {
                "policy": (
                    "buy_requires_signal_and_execution_membership;"
                    "official_after_close_change_effective_next_session;"
                    "research_force_exit_at_first_nonmember_session_open"
                ),
                "timeline_hash": point_in_time_timeline.timeline_hash,
                "price_roles": (
                    {
                        "signal_input": "research_adjusted",
                        "fills_and_valuation": (
                            "research_adjusted_compatibility_until_"
                            "corporate_action_state_machine_exists"
                        ),
                        "raw_execution": (
                            "bound_and_snapshotted_but_not_consumed"
                        ),
                    }
                    if raw_execution_pivot is not None
                    else {
                        "signal_input": (
                            "adjusted_research_compatibility"
                        ),
                        "fills_and_valuation": (
                            "adjusted_research_compatibility_"
                            "not_raw_execution"
                        ),
                    }
                ),
                "membership_exit_policy": "research_next_session_open",
                "execution_certified": False,
                "limitations": [
                    "not_index_reconstitution_tracking",
                    "not_raw_close_auction_execution",
                    "raw_effective_close_auction_not_implemented",
                    "corporate_action_runtime_application_missing",
                ],
                "canonical_price_binding": runtime_price_binding,
            }
        manifest_experiment = {
            **dict(exp),
            "data_access_policy": data_access_policy,
            "research_trust_profile": research_trust_profile,
        }
        run_manifest = build_run_manifest(
            experiment=manifest_experiment,
            strategy=strategy,
            strategy_metadata=strategy_meta,
            params=params,
            dataset_version=dataset_version,
            universe_snapshot=universe_snapshot,
            benchmark=benchmark_manifest,
            market_data_quality=market_data_quality,
            dataset_snapshot=dataset_snapshot,
            execution=execution_manifest,
            research_trust=(
                research_trust
                if research_trust_profile == "tushare_research_trusted"
                else None
            ),
            replay={
                "source_manifest_hash": replay_spec.get(
                    "source_manifest_hash"
                ),
                "allow_environment_drift": bool(
                    replay_spec.get("allow_environment_drift", False)
                ),
                "environment_differences": replay_spec.get(
                    "environment_differences",
                    [],
                ),
            } if replay_spec else {},
        )
        persisted_manifest = await persist_initial_manifest(
            db_path=db_path,
            experiment_id=exp_id,
            user_id=int(exp["user_id"]),
            manifest=run_manifest,
        )
        manifest_hash = persisted_manifest["manifest_hash"]
        if replay_spec:
            replay_differences = expected_replay_differences(
                run_manifest,
                replay_spec,
            )
            if replay_differences:
                raise ManifestDriftError(replay_differences)
        if not market_data_quality.is_clean:
            fatal_codes = ", ".join(market_data_quality.fatal_codes)
            raise ValueError(
                "行情数据质量硬门禁失败，策略未执行："
                f"{fatal_codes or 'unknown_market_data_failure'}"
            )

        await broker.update_job_progress(
            job_uuid,
            progress=0.15,
            message="生成策略信号并执行回测",
            stage="backtesting",
        )
        await broker.raise_if_cancelled(job_uuid)
        from backend.strategies.ml.runtime import (
            preload_strategy_native_runtime,
        )

        # Native ML libraries must be loaded on this event-loop thread before
        # the CPU-bound strategy work enters the executor. In particular,
        # first importing LightGBM from the executor can segfault on macOS.
        from backend.strategies.base import TrainableStrategy as _Trainable

        # Current trainable strategies are blocked until platform-owned PIT
        # sample and label masks exist; do not load native runtimes first.
        if not isinstance(strategy, _Trainable):
            preload_strategy_native_runtime(exp["strategy_id"])
        loop = asyncio.get_running_loop()

        def _cpu_work():
            from dataclasses import asdict

            from backend.strategies.base import TrainableStrategy
            from backend.strategies.research_context import (
                StrategyResearchContext,
                activate_research_context,
                validate_strategy_research_context,
            )

            wf_result = None
            research_context = (
                StrategyResearchContext.point_in_time_universe(
                    dates=point_in_time_timeline.dates,
                    members_by_date=point_in_time_timeline.members_by_date,
                    timeline_hash=point_in_time_timeline.timeline_hash,
                    price_role=(
                        "adjusted_research_compatibility_not_raw_execution"
                    ),
                )
                if point_in_time_timeline is not None
                else None
            )
            validate_strategy_research_context(
                requires_training=bool(strategy_meta.requires_training),
                trainable_protocol=isinstance(strategy, TrainableStrategy),
                context=research_context,
                point_in_time_capability=(
                    getattr(
                        strategy,
                        "point_in_time_context_capability",
                        None,
                    )
                ),
            )
            if isinstance(strategy, TrainableStrategy):
                # ``run_walk_forward`` below is an in-process implementation.
                # The current PIT mask gate already rejects every trainable
                # strategy, but retain this explicit guard so a future mask
                # implementation cannot accidentally enable unbounded fit on
                # the API worker. Periodic deployment retraining uses the
                # registered isolated CPU task; full experiment training needs
                # its own reviewed isolated result contract before activation.
                raise RuntimeError(
                    "训练型实验尚未接入隔离执行契约，已安全拒绝；"
                    "请勿绕过 PIT/隔离门禁"
                )
            with activate_research_context(research_context):
                if isinstance(strategy, TrainableStrategy):
                    # This branch remains structurally available for the future
                    # platform-owned sample+label mask contract. The validator
                    # above blocks every current TrainableStrategy before fit.
                    from backend.services.walkforward import (
                        WalkForwardCancelled,
                        run_walk_forward,
                    )

                    def _wf_progress(frac: float, msg: str) -> None:
                        try:
                            asyncio.run_coroutine_threadsafe(
                                broker.update_job_progress(
                                    job_uuid,
                                    progress=0.15
                                    + 0.55 * max(0.0, min(frac, 1.0)),
                                    message=msg,
                                    stage="backtesting",
                                ),
                                loop,
                            )
                        except Exception:
                            pass  # progress failure does not affect execution

                    def _wf_cancelled() -> bool:
                        try:
                            return bool(
                                asyncio.run_coroutine_threadsafe(
                                    broker.is_cancel_requested(job_uuid), loop
                                ).result(timeout=10)
                            )
                        except Exception:
                            return False

                    try:
                        wf_result = run_walk_forward(
                            strategy,
                            pivot,
                            runtime_params,
                            exp["test_start"],
                            exp["test_end"],
                            progress_callback=_wf_progress,
                            cancel_callback=_wf_cancelled,
                        )
                    except WalkForwardCancelled as exc:
                        raise JobCancelledError("任务已取消") from exc
                    signals = wf_result.signals
                else:
                    signals = strategy.generate_batch_signals(
                        pivot,
                        runtime_params,
                        exp["test_start"],
                        exp["test_end"],
                    )
            if point_in_time_timeline is not None:
                from backend.data.point_in_time_universe import (
                    validate_signals_against_timeline,
                )

                validate_signals_against_timeline(
                    signals,
                    point_in_time_timeline,
                )
            if strategy_meta.requires_training and not any(signals.values()):
                raise RuntimeError("训练型策略未生成任何信号；请检查训练窗口和训练日志")

            from backend.core.cost_model import CostModel
            from backend.core.engine import BacktestEngine, ExecutionConstraints
            from backend.core.metrics import compute_all_metrics

            engine = BacktestEngine(
                initial_capital=execution_manifest["initial_capital"],
                cost_model=CostModel(**execution_manifest["cost_model"]),
                start_date=exp["test_start"],
                end_date=exp["test_end"],
                max_positions=execution_manifest["max_positions"],
                rebalance_mode=execution_manifest["rebalance_mode"],
                portfolio_signal_mode=execution_manifest[
                    "portfolio_signal_mode"
                ],
                execution_constraints=ExecutionConstraints(
                    **execution_manifest["execution_constraints"]
                ),
                eligible_codes_by_date=(
                    {
                        day: set(members)
                        for day, members in zip(
                            point_in_time_timeline.dates,
                            point_in_time_timeline.members_by_date,
                        )
                    }
                    if point_in_time_timeline is not None
                    else None
                ),
                membership_exit_policy="research_next_session_open",
            )
            result = engine.run(
                signals,
                pivot,
                strategy_id=exp["strategy_id"],
            )
            benchmark_equity = None
            if not benchmark_close.empty:
                target_index = pd.DatetimeIndex(result.equity_curve.index)
                aligned_close = (
                    benchmark_close.reindex(
                        benchmark_close.index.union(target_index)
                    )
                    .sort_index()
                    .ffill()
                    .reindex(target_index)
                    .dropna()
                )
                prior_close = benchmark_close[
                    benchmark_close.index < target_index.min()
                ]
                benchmark_base = (
                    float(prior_close.iloc[-1])
                    if not prior_close.empty
                    else (
                        float(aligned_close.iloc[0])
                        if not aligned_close.empty
                        else 0.0
                    )
                )
                if not aligned_close.empty and benchmark_base > 0:
                    benchmark_equity = (
                        aligned_close / benchmark_base
                    ) * 1_000_000
                    result.equity_curve["benchmark"] = benchmark_equity.reindex(
                        result.equity_curve.index
                    )
            metrics = compute_all_metrics(
                result.equity_curve,
                benchmark_equity,
                result.trade_log,
            )
            if "error" in metrics:
                raise RuntimeError(str(metrics["error"]))

            artifact = None
            trained_model = getattr(strategy, "_model", None)
            if trained_model is not None:
                from backend.services.ml_promotion_evidence import (
                    build_model_promotion_evidence,
                    finite_json_value,
                )
                from backend.services.model_serialization import (
                    contract_for_model,
                )

                if wf_result is not None:
                    retrained_cycles = [
                        cycle for cycle in wf_result.cycles if cycle.retrained
                    ]
                    cycles_payload = [
                        asdict(cycle) for cycle in wf_result.cycles
                    ]
                    runtime_telemetry = {}
                    train_samples_values = [
                        cycle.n_train_samples
                        for cycle in retrained_cycles
                        if cycle.n_train_samples is not None
                    ]
                    feature_count_values = [
                        cycle.n_train_features
                        for cycle in retrained_cycles
                        if cycle.n_train_features is not None
                    ]
                else:
                    telemetry_reader = getattr(
                        strategy, "get_training_telemetry", None
                    )
                    runtime_telemetry = (
                        telemetry_reader()
                        if callable(telemetry_reader)
                        else {}
                    )
                    cycles_payload = list(
                        runtime_telemetry.get("cycles") or []
                    )
                    retrained_cycles = [
                        cycle
                        for cycle in cycles_payload
                        if cycle.get("retrained") and not cycle.get("error")
                    ]
                    train_samples_values = [
                        int(cycle["n_train_samples"])
                        for cycle in retrained_cycles
                        if cycle.get("n_train_samples") is not None
                    ]
                    feature_count_values = [
                        int(cycle["n_train_features"])
                        for cycle in retrained_cycles
                        if cycle.get("n_train_features") is not None
                    ]
                train_telemetry = {
                    "training_mode": (
                        "train_once"
                        if strategy_meta.retrain_frequency.value == "never"
                        else "periodic"
                    ),
                    "retrain_frequency": strategy_meta.retrain_frequency.value,
                    "retrain_count": len(retrained_cycles),
                    "total_fit_samples": (
                        sum(train_samples_values) if train_samples_values else None
                    ),
                    "elapsed_seconds": (
                        wf_result.elapsed_seconds if wf_result is not None else None
                    ),
                    "summary": wf_result.summary() if wf_result is not None else None,
                    "last_training_window": (
                        list(wf_result.last_window)
                        if wf_result is not None and wf_result.last_window
                        else None
                    ),
                    "last_validation_window": (
                        list(wf_result.last_validation_window)
                        if wf_result is not None and wf_result.last_validation_window
                        else None
                    ),
                    "cycles": (
                        cycles_payload
                    ),
                }
                if runtime_telemetry:
                    train_telemetry.update(runtime_telemetry)
                train_telemetry = finite_json_value(train_telemetry)
                model_dir = settings.abs_path(f"{settings.MODEL_STORE_DIR}/experiment_{exp_id}")
                model_path = model_dir / "model_v1.joblib"
                metadata_path = model_dir / "model_v1.json"
                strategy.save_model(trained_model, str(model_path))
                serialization = contract_for_model(strategy, trained_model)
                with model_path.open("rb") as model_file:
                    model_sha256 = hashlib.file_digest(
                        model_file,
                        "sha256",
                    ).hexdigest()
                metadata_path.write_text(
                    json.dumps(
                        {
                            "experiment_id": exp_id,
                            "strategy_id": exp["strategy_id"],
                            "params": params,
                            "data_version": data_version,
                            "train_start": exp["train_start"],
                            "train_end": exp["train_end"],
                            "training": train_telemetry,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                promotion_evidence = build_model_promotion_evidence(
                    experiment=dict(exp),
                    strategy=strategy,
                    strategy_metadata=strategy_meta,
                    params=params,
                    walkforward_result=wf_result,
                    model_version=1,
                    model_sha256=model_sha256,
                    model_size=model_path.stat().st_size,
                    metadata_file_path=str(metadata_path),
                    run_manifest_hash=manifest_hash,
                    training_telemetry=train_telemetry,
                    model_serialization=serialization,
                )
                artifact = {
                    "model_path": str(model_path),
                    "metadata_path": str(metadata_path),
                    "model_sha256": model_sha256,
                    "model_size": model_path.stat().st_size,
                    "train_samples": (
                        train_samples_values[-1] if train_samples_values else None
                    ),
                    "feature_count": (
                        max(feature_count_values) if feature_count_values else None
                    ),
                    "train_metrics": train_telemetry,
                    "promotion_evidence": promotion_evidence,
                    "train_window_start": (
                        wf_result.last_window[0]
                        if wf_result is not None and wf_result.last_window
                        else (
                            (train_telemetry.get("last_training_window") or [None])[0]
                            or exp["train_start"]
                        )
                    ),
                    "train_window_end": (
                        wf_result.last_window[1]
                        if wf_result is not None and wf_result.last_window
                        else (
                            (train_telemetry.get("last_training_window") or [None, None])[1]
                            or exp["train_end"]
                        )
                    ),
                }
            return result, metrics, artifact

        result, metrics, artifact = await loop.run_in_executor(None, _cpu_work)
        await broker.raise_if_cancelled(job_uuid)
        await broker.update_job_progress(
            job_uuid,
            progress=0.75,
            message="保存净值、成交和指标",
            stage="persisting_results",
        )

        equity = result.equity_curve.copy()
        equity["daily_return"] = equity["equity"].pct_change()
        equity["drawdown"] = equity["equity"] / equity["equity"].cummax() - 1
        equity_rows = [
            (
                exp_id,
                pd.Timestamp(index).strftime("%Y-%m-%d"),
                float(row["equity"]),
                _safe_metric({"value": row.get("benchmark")}, "value"),
                _safe_metric({"value": row.get("daily_return")}, "value"),
                _safe_metric({"value": row.get("drawdown")}, "value"),
            )
            for index, row in equity.iterrows()
        ]
        trade_rows = [
            (
                exp_id,
                trade.date,
                trade.signal_date,
                trade.code,
                trade.action,
                trade.price,
                trade.shares,
                trade.amount,
                trade.cost,
                trade.signal_strategy,
                trade.signal_score,
            )
            for trade in result.trade_log
        ]

        metric_columns = [
            "cumulative_return", "sharpe_ratio", "annual_return", "max_drawdown",
            "volatility", "calmar_ratio", "sortino_ratio", "win_rate",
            "profit_loss_ratio", "avg_trade_return", "max_consecutive_wins",
            "max_consecutive_losses", "total_trades", "avg_holding_days",
            "turnover_rate", "information_ratio", "treynor_ratio", "alpha",
            "beta", "tracking_error", "upside_capture", "downside_capture",
            "var_95", "cvar_95", "skewness", "kurtosis", "daily_sharpe",
            "monthly_sharpe", "yearly_return", "recovery_days",
            "max_drawdown_duration", "avg_drawdown", "avg_drawdown_days",
            "best_month", "worst_month", "positive_months", "profit_factor",
            "expectency",
        ]
        metric_keys = {
            "annual_return": ("annualized_return",),
            "volatility": ("annualized_volatility",),
            "profit_loss_ratio": ("win_loss_ratio",),
            "skewness": ("return_skewness",),
            "kurtosis": ("return_kurtosis",),
            "yearly_return": ("annualized_return",),
            "recovery_days": ("max_drawdown_recovery_days",),
            "positive_months": ("monthly_win_rate",),
        }
        metric_values = [
            _safe_metric(metrics, *(metric_keys.get(column) or (column,)))
            for column in metric_columns
        ]

        async with aiosqlite.connect(db_path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("BEGIN")
            await db.execute("DELETE FROM equity_curve WHERE experiment_id = ?", (exp_id,))
            await db.execute("DELETE FROM trade_log WHERE experiment_id = ?", (exp_id,))
            await db.execute("DELETE FROM experiment_metrics WHERE experiment_id = ?", (exp_id,))
            await db.execute("DELETE FROM model_artifacts WHERE experiment_id = ?", (exp_id,))
            if equity_rows:
                await db.executemany(
                    """
                    INSERT INTO equity_curve
                        (experiment_id, date, equity, benchmark, daily_return, drawdown)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    equity_rows,
                )
            if trade_rows:
                await db.executemany(
                    """
                    INSERT INTO trade_log
                        (experiment_id, date, signal_date, code, action, price,
                         shares, amount, cost, signal_strategy, signal_score)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    trade_rows,
                )
            placeholders = ",".join("?" for _ in range(len(metric_columns) + 1))
            await db.execute(
                f"""
                INSERT INTO experiment_metrics (experiment_id, {",".join(metric_columns)})
                VALUES ({placeholders})
                """,
                [exp_id, *metric_values],
            )
            if artifact is not None:
                await db.execute(
                    """
                    INSERT INTO model_artifacts
                        (experiment_id, strategy_id, model_version, model_file_path,
                         metadata_file_path, params_hash, train_window_start,
                         train_window_end, feature_count, train_samples,
                         train_metrics, artifact_sha256, artifact_size,
                         run_manifest_hash, is_latest)
                    VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                    """,
                    (
                        exp_id,
                        exp["strategy_id"],
                        artifact["model_path"],
                        artifact["metadata_path"],
                        exp["params_hash"],
                        artifact["train_window_start"],
                        artifact["train_window_end"],
                        artifact["feature_count"],
                        artifact["train_samples"],
                        json.dumps(artifact["train_metrics"], ensure_ascii=False),
                        artifact["model_sha256"],
                        artifact["model_size"],
                        manifest_hash,
                    ),
                )
                await db.execute(
                    """
                    INSERT OR IGNORE INTO research_artifact_manifests
                        (experiment_id, run_manifest_hash, schema_version,
                         artifact_kind, artifact_sha256, artifact_size,
                         metadata_json, created_at)
                    VALUES (?, ?, 'research-artifact-manifest/v1',
                            'trained_model', ?, ?, ?, datetime('now'))
                    """,
                    (
                        exp_id,
                        manifest_hash,
                        artifact["model_sha256"],
                        artifact["model_size"],
                        json.dumps(
                            artifact["promotion_evidence"],
                            ensure_ascii=False,
                            sort_keys=True,
                            allow_nan=False,
                        ),
                    ),
                )
            await db.execute(
                """
                UPDATE experiments
                SET status='completed', progress_pct=100,
                    progress_message='回测完成', completed_at=datetime('now'),
                    data_version=?
                WHERE id=?
                """,
                (data_version, exp_id),
            )
            await _refresh_related_sweep_status(db, exp_id)
            await db.commit()

        await broker.update_job_progress(
            job_uuid,
            progress=1.0,
            status="completed",
            message="回测完成",
            stage="completed",
            result={
                "experiment_id": exp_id,
                "data_version": data_version,
                "equity_points": len(equity_rows),
                "trades": len(trade_rows),
                "sharpe_ratio": metrics.get("sharpe_ratio"),
                "annual_return": metrics.get("annualized_return"),
            },
        )
    except JobCancelledError:
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                """
                UPDATE experiments
                SET status='cancelled', progress_message='任务已取消',
                    completed_at=datetime('now')
                WHERE id=?
                """,
                (exp_id,),
            )
            await _refresh_related_sweep_status(db, exp_id)
            await db.commit()
    except Exception:
        logger.exception("Experiment %d failed", exp_id)
        import traceback
        from backend.core.security_boundaries import sanitize_diagnostic

        error_traceback = sanitize_diagnostic(
            traceback.format_exc(), max_length=16_384
        )
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                """
                UPDATE experiments
                SET status='failed', progress_message='执行失败',
                    error_log=?, completed_at=datetime('now')
                WHERE id=?
                """,
                (error_traceback, exp_id),
            )
            await _refresh_related_sweep_status(db, exp_id)
            await db.commit()
        await broker.update_job_progress(
            job_uuid,
            progress=1.0,
            status="failed",
            error=error_traceback,
            message="执行失败",
            stage="failed",
        )


async def _execute_job(job: dict) -> None:
    """Execute one already-claimed job inside the scheduler lease boundary."""
    from backend.jobs.broker import get_broker

    broker = get_broker()
    job_uuid = str(job["job_uuid"])
    job_type = job.get("job_type")
    params = job.get("params", {})
    if job_type == "backtest":
        exp_id = params.get("experiment_id")
        if not exp_id:
            raise ValueError("backtest job missing experiment_id")
        await _run_experiment(int(exp_id), job_uuid)
    elif job_type == "daily_simulation":
        from backend.services.simulation import run_daily_simulation

        required_data_job_uuid = params.get("required_data_job_uuid")
        if required_data_job_uuid:
            await broker.require_completed_job(
                str(required_data_job_uuid),
                expected_type="data_update",
            )
        await broker.raise_if_cancelled(job_uuid)
        result = await run_daily_simulation(
            int(job.get("user_id") or params.get("user_id")),
            params.get("date"),
            portfolio_id=params.get("portfolio_id"),
        )
        await broker.raise_if_cancelled(job_uuid)
        await broker.update_job_progress(
            job_uuid,
            progress=1.0,
            status="completed",
            result=result,
            message="模拟盘日结完成",
            stage="completed",
        )
    elif job_type == "simulation_backfill":
        from backend.services.simulation import run_simulation_backfill

        async def report_backfill_progress(progress: float) -> None:
            await broker.raise_if_cancelled(job_uuid)
            await broker.update_job_progress(
                job_uuid,
                progress=progress,
                message="正在回放历史交易日",
                stage="simulation_backfill",
            )

        await broker.raise_if_cancelled(job_uuid)
        result = await run_simulation_backfill(
            int(job.get("user_id") or params.get("user_id")),
            str(params["start_date"]),
            str(params["end_date"]),
            report_backfill_progress,
            portfolio_id=params.get("portfolio_id"),
            restart=bool(params.get("restart", False)),
        )
        await broker.raise_if_cancelled(job_uuid)
        await broker.update_job_progress(
            job_uuid,
            progress=1.0,
            status="completed",
            result=result,
            message="历史回放完成",
            stage="completed",
        )
    elif job_type == "pit_durable_update":
        from backend.services.pit_durable_update import run_configured_pit_update

        await broker.update_job_progress(
            job_uuid,
            progress=0.05,
            message="PIT durable update state machine running",
            stage="updating_data",
        )
        await broker.raise_if_cancelled(job_uuid)
        result = await run_configured_pit_update(str(params["idempotency_key"]))
        await broker.raise_if_cancelled(job_uuid)
        await broker.update_job_progress(
            job_uuid,
            progress=1.0,
            status="completed",
            result=result,
            message="PIT durable update checkpoint committed",
            stage="completed",
        )
    elif job_type == "candidate_data_preflight":
        from backend.services.candidate_preflight_scheduler import (
            CandidatePreflightJobError,
            run_candidate_preflight_job,
        )

        await broker.update_job_progress(
            job_uuid,
            progress=0.05,
            message="候选数据预检正在采集隔离证据",
            stage="updating_data",
        )
        await broker.raise_if_cancelled(job_uuid)
        try:
            result = await run_candidate_preflight_job(params)
        except CandidatePreflightJobError as exc:
            await broker.update_job_progress(
                job_uuid,
                result=exc.public_result(),
                message="候选数据预检失败；隔离区与结构化诊断已保留",
                stage="candidate_preflight_failed",
            )
            raise
        await broker.raise_if_cancelled(job_uuid)
        deferred = result.get("preflight_outcome") == (
            "deferred_insufficient_coverage"
        )
        await broker.update_job_progress(
            job_uuid,
            progress=1.0,
            status="completed",
            result=result,
            message=(
                "候选数据预检已延后：最新完整月覆盖不足；证据仅保留在隔离区"
                if deferred
                else "候选数据预检完成；证据仅保留在隔离区"
            ),
            stage="candidate_preflight_deferred" if deferred else "completed",
        )
    elif job_type == "research_data_refresh":
        from backend.services.research_data_refresh import (
            ResearchDataRefreshError,
            run_research_data_refresh_responsive,
        )

        await broker.update_job_progress(
            job_uuid,
            progress=0.02,
            message="正在准备有界研究数据刷新",
            stage="provider_collection",
        )

        async def report_research_refresh(event: dict[str, Any]) -> None:
            update = {
                "progress": max(
                    0.02, min(float(event["overall_fraction"]), 0.99)
                ),
                "message": str(event["message"]),
                "stage": str(event["stage"]),
            }
            if event.get("cancellable") is False:
                # Enter the explicit atomic commit point first. request_cancel
                # rejects later cancellation while this stage is visible; an
                # earlier request remains detectable before file activation.
                await broker.update_job_progress(job_uuid, **update)
                await broker.raise_if_cancelled(job_uuid)
            else:
                await broker.raise_if_cancelled(job_uuid)
                await broker.update_job_progress(job_uuid, **update)

        try:
            result = await run_research_data_refresh_responsive(
                source_id=str(params.get("source_id") or ""),
                from_month=str(params.get("from_month") or "2016-01"),
                to_month=(
                    str(params["to_month"])
                    if params.get("to_month") is not None
                    else None
                ),
                max_calls=int(params.get("max_calls") or 16),
                retry_optional_failures=not bool(params.get("continuation_of")),
                progress=report_research_refresh,
            )
        except ResearchDataRefreshError as exc:
            await broker.update_job_progress(
                job_uuid,
                result=exc.result,
                message=str(exc),
                stage="research_import",
            )
            raise
        if result.get("activation_committed") is not True:
            await broker.raise_if_cancelled(job_uuid)
        if result.get("continuation_required") is True:
            try:
                raw_user_id = int(params.get("user_id") or 0)
                continuation_job_id = await broker.submit_job(
                    job_type="research_data_refresh",
                    params={
                        **params,
                        "max_calls": 128,
                        "continuation_of": job_uuid,
                    },
                    user_id=raw_user_id if raw_user_id > 0 else None,
                    resource_type="research_data_source",
                    resource_id=str(params.get("source_id") or "tushare"),
                    deduplicate_active=False,
                )
                result["continuation_job_id"] = continuation_job_id
                result["continuation_scheduled"] = True
            except Exception as exc:
                logger.warning(
                    "Unable to schedule research refresh continuation: %s",
                    type(exc).__name__,
                )
                result["continuation_scheduled"] = False
        await broker.update_job_progress(
            job_uuid,
            progress=1.0,
            status="completed",
            result=result,
            message=(
                "研究数据本批次完成并已自动安排续跑；风险与跨源差异保留为告警"
                if result.get("continuation_scheduled")
                else "研究数据刷新完成；风险与跨源差异已保留为告警"
            ),
            stage="completed",
        )
    elif job_type == "pit_governance_refresh":
        from backend.services.maintenance import (
            DataUpdateFailedError,
            run_pit_governance_refresh,
        )

        await broker.update_job_progress(
            job_uuid,
            progress=0.05,
            message="正在刷新官方 PIT 治理证据（不更新行情或双价格账本）",
            stage="pit_governance_collection",
        )
        await broker.raise_if_cancelled(job_uuid)
        try:
            result = await run_pit_governance_refresh(
                params.get("pool_id"),
                actor_user_id=int(
                    job.get("user_id")
                    or params.get("user_id")
                    or params.get("actor_user_id")
                    or 0
                ),
            )
        except DataUpdateFailedError as exc:
            await broker.update_job_progress(
                job_uuid,
                result=exc.result,
                message="PIT 治理证据刷新失败；未触发行情更新",
                stage="pit_governance_collection",
            )
            raise
        await broker.raise_if_cancelled(job_uuid)
        await broker.update_job_progress(
            job_uuid,
            progress=1.0,
            status="completed",
            result=result,
            message="PIT 治理证据已写入隔离区，等待独立复核；行情未更新",
            stage="completed",
        )
    elif job_type == "data_update":
        from backend.services.maintenance import (
            DataUpdateFailedError,
            run_data_update,
        )

        await broker.update_job_progress(
            job_uuid,
            progress=0.1,
            message="正在核验 PIT 双价格账本更新条件",
            stage="updating_data",
        )
        await broker.raise_if_cancelled(job_uuid)
        reported_progress = 0.1

        async def report_market_data_progress(event: dict[str, Any]) -> None:
            nonlocal reported_progress
            await broker.raise_if_cancelled(job_uuid)
            completed = max(0, int(event.get("completed_codes", 0)))
            total = max(0, int(event.get("total_codes", 0)))
            source_role = str(event.get("source_role", "validation"))
            provider = str(event.get("provider", "unknown"))
            reused = bool(event.get("reused_staging", False))
            overall = max(
                0.0,
                min(float(event.get("overall_fraction", 0.0)), 1.0),
            )
            reported_progress = max(reported_progress, 0.1 + 0.8 * overall)
            role_label = {
                "primary": "主源",
                "reference": "复核源",
                "adjusted_reference": "复权差异观察源",
                "validation": "双源核验",
                "execution_binding": "双价格账本门禁",
            }.get(source_role, source_role)
            reuse_label = "（已恢复安全暂存）" if reused else ""
            await broker.update_job_progress(
                job_uuid,
                progress=reported_progress,
                result={
                    "market_data_progress": {
                        "source_role": source_role,
                        "provider": provider,
                        "completed_codes": completed,
                        "total_codes": total,
                        "reused_staging": reused,
                    }
                },
                message=(
                    f"{role_label} {provider}{reuse_label}："
                    f"{completed}/{total} 只股票"
                ),
                stage=(
                    "market_data_execution_binding"
                    if source_role == "execution_binding"
                    else f"market_data_{source_role}"
                ),
            )

        try:
            result = await run_data_update(
                params.get("pool_id"),
                progress=report_market_data_progress,
                actor_user_id=int(
                    job.get("user_id")
                    or params.get("user_id")
                    or params.get("actor_user_id")
                    or 0
                ),
            )
        except DataUpdateFailedError as exc:
            await broker.update_job_progress(
                job_uuid,
                result=exc.result,
                message="PIT 行情/双价格账本更新已阻断；运行时数据未变更",
                stage="market_data_failed_validation",
            )
            raise
        await broker.raise_if_cancelled(job_uuid)
        await broker.update_job_progress(
            job_uuid,
            progress=1.0,
            status="completed",
            result=result,
            message="PIT 证据采集完成，等待独立复核与激活",
            stage="completed",
        )
    elif job_type == "retrain":
        from backend.services.maintenance import retrain_deployment

        await broker.update_job_progress(
            job_uuid,
            progress=0.1,
            message="正在重新训练模型",
            stage="training",
        )
        await broker.raise_if_cancelled(job_uuid)
        result = await retrain_deployment(
            int(params["deployment_id"]),
            int(job.get("user_id") or params["user_id"]),
        )
        await broker.raise_if_cancelled(job_uuid)
        await broker.update_job_progress(
            job_uuid,
            progress=1.0,
            status="completed",
            result=result,
            message="模型重训练完成",
            stage="completed",
        )
    elif job_type == "factor_research":
        from pydantic import ValidationError

        from backend.services.factor_research import (
            FactorResearchBody,
            FactorResearchExecutionError,
            execute_factor_research,
        )

        owner_user_id = job.get("user_id")

        async def report_factor_progress(
            progress: float,
            message: str,
            stage: str,
        ) -> None:
            await broker.raise_if_cancelled(job_uuid)
            await broker.update_job_progress(
                job_uuid,
                progress=progress,
                message=message,
                stage=stage,
            )

        try:
            body = FactorResearchBody.model_validate(params)
            result = await execute_factor_research(
                body,
                owner_user_id=int(owner_user_id or 0),
                progress=report_factor_progress,
                source_job_uuid=job_uuid,
            )
            await broker.raise_if_cancelled(job_uuid)
        except ValidationError:
            error = FactorResearchExecutionError(
                code="factor_research_request_invalid",
                message="因子研究任务参数无效，无法安全执行",
            )
            await broker.update_job_progress(
                job_uuid,
                progress=1.0,
                status="failed",
                result=error.public_result(),
                error=error.message,
                message=error.message,
                stage="failed",
            )
            return
        except FactorResearchExecutionError as exc:
            await broker.update_job_progress(
                job_uuid,
                progress=1.0,
                status="failed",
                result=exc.public_result(),
                error=exc.message,
                message=exc.message,
                stage="failed",
            )
            return
        run = result["run"]
        await broker.update_job_progress(
            job_uuid,
            progress=1.0,
            status="completed",
            result={
                "run_id": run["run_id"],
                "dataset_digest": run["dataset_digest"],
                "result_digest": run["result_digest"],
            },
            message="因子研究完成并保存不可变证据",
            stage="completed",
        )
    else:
        raise ValueError(f"unsupported job type: {job_type}")


async def _job_worker() -> None:
    """Run the resource-aware, lease-protected local dispatcher."""
    from backend.jobs.broker import get_broker
    from backend.jobs.scheduler import AdaptiveJobScheduler

    scheduler = AdaptiveJobScheduler(get_broker(), _execute_job)
    await scheduler.run()


async def _supervise_job_worker(
    *,
    max_attempts: int = 3,
    retry_base_seconds: float = 1.0,
    stable_run_seconds: float = 60.0,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """Restart an unexpectedly failed dispatcher without hiding a crash loop."""
    attempts = max(int(max_attempts), 1)
    consecutive_failures = 0
    while True:
        started_at = monotonic()
        try:
            await _job_worker()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure = exc
        else:
            failure = RuntimeError(
                "background job worker stopped unexpectedly without an error"
            )

        uptime = monotonic() - started_at
        if (
            consecutive_failures
            and uptime >= max(float(stable_run_seconds), 0.0)
        ):
            consecutive_failures = 0
        consecutive_failures += 1
        logger.exception(
            "Background job worker crashed (consecutive attempt %d/%d)",
            consecutive_failures,
            attempts,
            exc_info=(
                type(failure),
                failure,
                failure.__traceback__,
            ),
        )
        if consecutive_failures >= attempts:
            logger.critical(
                "Background job worker exhausted %d consecutive restart attempts",
                attempts,
            )
            raise RuntimeError(
                "background job worker exhausted its restart budget"
            ) from failure
        delay = max(float(retry_base_seconds), 0.0) * (
            2 ** (consecutive_failures - 1)
        )
        logger.warning("Restarting background job worker in %.2fs", delay)
        await asyncio.sleep(delay)


def _stopped_critical_background_tasks(app: FastAPI) -> list[str]:
    """Return stable public component names without exposing task exceptions."""
    critical_tasks = getattr(app.state, "critical_background_tasks", {})
    return sorted(name for name, task in critical_tasks.items() if task.done())


def _worker_heartbeat_health(
    app: FastAPI,
    *,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Assess in-memory heartbeat freshness without a database operation."""
    broker = getattr(app.state, "job_broker", None)
    started_monotonic = getattr(
        app.state,
        "critical_background_started_monotonic",
        None,
    )
    grace_seconds = max(
        min(float(settings.JOB_SCHEDULER_LEASE_SECONDS), 60.0),
        15.0,
    )
    startup_grace = (
        started_monotonic is None
        or monotonic() - float(started_monotonic) < grace_seconds
    )
    if broker is None:
        return {
            "healthy": startup_grace,
            "online": False,
            "startup_grace": startup_grace,
            "standby": False,
            "heartbeat_at": None,
        }

    snapshot = broker.worker_health_snapshot()
    reasons = list(snapshot.get("reasons") or [])
    standby = (
        snapshot.get("leader") is False
        and "scheduler_lease_held_by_other_process" in reasons
    )
    online = snapshot.get("online") is True
    return {
        "healthy": online or startup_grace or standby,
        "online": online,
        "startup_grace": startup_grace,
        "standby": standby,
        "heartbeat_at": snapshot.get("heartbeat_at"),
    }


async def _shutdown_background_runtime(
    tasks: dict[str, asyncio.Task[None]],
    broker: Any,
) -> None:
    """Stop every task and always run durable broker cleanup."""
    for task in tasks.values():
        if not task.done():
            task.cancel()
    try:
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for (name, _), result in zip(tasks.items(), results, strict=True):
            if isinstance(result, asyncio.CancelledError):
                continue
            if isinstance(result, BaseException):
                logger.error(
                    "Background task %s exited with an error before shutdown",
                    name,
                    exc_info=(type(result), result, result.__traceback__),
                )
    finally:
        try:
            await broker.shutdown()
        except Exception:
            logger.exception("Broker shutdown error")


# ═══════════════════════════════════════════════════════════════════════════
# 生命周期
# ═══════════════════════════════════════════════════════════════════════════


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时初始化，关闭时清理。"""
    # 启动
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    install_sensitive_url_log_filter()
    logger.info("Starting Quant Platform V3...")
    if settings.ENVIRONMENT == "production":
        if settings.JWT_SECRET == "change-me-in-production":
            raise RuntimeError("生产环境必须配置安全的 JWT_SECRET")
        if len(settings.JWT_SECRET.encode("utf-8")) < 32:
            raise RuntimeError("生产环境 JWT_SECRET 必须至少包含 32 个 UTF-8 字节")

    await _init_databases()
    await _scan_strategies()

    # Start background workers. The scheduler only enqueues durable jobs, so
    # the execution path is identical for manual and automatic simulations.
    from backend.jobs.broker import get_broker
    broker = get_broker()
    app.state.job_broker = broker
    await broker.record_operational_event("service_start", "service")
    structured_log(
        logger,
        logging.INFO,
        "service_start",
        component="api",
    )
    app.state.critical_background_started_monotonic = time.monotonic()
    worker_task = asyncio.create_task(
        _supervise_job_worker(),
        name="job-worker-supervisor",
    )
    from backend.services.simulation_scheduler import run_paper_simulation_scheduler
    scheduler_task = asyncio.create_task(run_paper_simulation_scheduler())
    from backend.services.model_lifecycle import run_model_retrain_scheduler
    model_scheduler_task = asyncio.create_task(
        run_model_retrain_scheduler(),
        name="model-retrain-scheduler",
    )
    from backend.services.pit_automation_scheduler import (
        run_pit_automation_scheduler,
    )
    pit_scheduler_task = asyncio.create_task(
        run_pit_automation_scheduler(),
        name="pit-automation-scheduler",
    )
    from backend.services.candidate_preflight_scheduler import (
        run_candidate_preflight_scheduler,
    )
    candidate_preflight_scheduler_task = asyncio.create_task(
        run_candidate_preflight_scheduler(),
        name="candidate-preflight-scheduler",
    )
    from backend.services.research_data_scheduler import (
        run_research_data_scheduler,
    )
    research_data_scheduler_task = asyncio.create_task(
        run_research_data_scheduler(),
        name="research-data-scheduler",
    )
    app.state.critical_background_tasks = {
        "job_worker": worker_task,
        **(
            {"paper_simulation_scheduler": scheduler_task}
            if settings.PAPER_SIMULATION_AUTO_RUN
            else {}
        ),
        **(
            {"model_retrain_scheduler": model_scheduler_task}
            if settings.MODEL_RETRAIN_AUTO_RUN
            else {}
        ),
        **(
            {"pit_automation_scheduler": pit_scheduler_task}
            if settings.PIT_AUTOMATION_AUTO_RUN
            else {}
        ),
        **(
            {
                "candidate_preflight_scheduler": (
                    candidate_preflight_scheduler_task
                )
            }
            if settings.PIT_CANDIDATE_PREFLIGHT_AUTO_RUN
            else {}
        ),
        **(
            {"research_data_scheduler": research_data_scheduler_task}
            if settings.RESEARCH_DATA_REFRESH_AUTO_RUN
            else {}
        ),
    }
    logger.info("Background worker started")

    try:
        yield
    finally:
        logger.info("Shutting down Quant Platform V3...")
        await broker.record_operational_event("service_stop", "service")
        structured_log(
            logger,
            logging.INFO,
            "service_stop",
            component="api",
        )
        await _shutdown_background_runtime(
            {
                "job_worker": worker_task,
                "paper_simulation_scheduler": scheduler_task,
                "model_retrain_scheduler": model_scheduler_task,
                "pit_automation_scheduler": pit_scheduler_task,
                "candidate_preflight_scheduler": (
                    candidate_preflight_scheduler_task
                ),
                "research_data_scheduler": research_data_scheduler_task,
            },
            broker,
        )
        logger.info("Shutdown complete.")


# ═══════════════════════════════════════════════════════════════════════════
# FastAPI 应用
# ═══════════════════════════════════════════════════════════════════════════

app = FastAPI(
    title="量化验证平台",
    version=APP_VERSION,
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"],
)
app.middleware("http")(attach_request_id)


# ── 注册 REST Router ───────────────────────────────────────────────────────

from backend.api.auth import router as auth_router
from backend.api.strategies import router as strategies_router
from backend.api.experiments import router as experiments_router
from backend.api.trading import router as trading_router
from backend.api.data import router as data_router
from backend.api.ai import router as ai_router
from backend.api.jobs import router as jobs_router
from backend.api.execution import router as execution_router
from backend.api.admin import router as admin_router
from backend.api.remote_training import router as remote_training_router
from backend.api.research import router as research_router
from backend.api.research_workflow import router as research_workflow_router
from backend.api.factor_research import router as factor_research_router
from backend.api.factor_research_protocols import (
    router as factor_research_protocols_router,
)
from backend.api.strategy_correlation import router as strategy_correlation_router
from backend.api.point_in_time import router as point_in_time_router
from backend.api.price_ledger import router as price_ledger_router
from backend.api.provider_licence_evidence import (
    router as provider_licence_evidence_router,
)

app.include_router(auth_router)
app.include_router(strategies_router)
app.include_router(experiments_router)
app.include_router(trading_router)
app.include_router(data_router)
app.include_router(ai_router)
app.include_router(jobs_router)
app.include_router(execution_router)
app.include_router(admin_router)
app.include_router(remote_training_router)
app.include_router(research_router)
app.include_router(research_workflow_router)
app.include_router(factor_research_router)
app.include_router(factor_research_protocols_router)
app.include_router(strategy_correlation_router)
app.include_router(point_in_time_router)
app.include_router(price_ledger_router)
app.include_router(provider_licence_evidence_router)


# ── 注册 WebSocket 端点 ────────────────────────────────────────────────────

from backend.ws.training import ws_endpoint as training_ws
from backend.ws.realtime import ws_endpoint as realtime_ws
from backend.ws.notifications import ws_endpoint as notifications_ws
from backend.ws.jobs import ws_endpoint as jobs_ws

app.add_api_websocket_route("/ws/training/{experiment_id}", training_ws)
app.add_api_websocket_route("/ws/realtime/{deployment_id}", realtime_ws)
app.add_api_websocket_route("/ws/notifications", notifications_ws)
app.add_api_websocket_route("/ws/jobs", jobs_ws)


# ── 健康检查 ────────────────────────────────────────────────────────────────

@app.get("/api/health")
async def health_check(request: Request, response: Response):
    """健康检查端点；关键后台任务退出时供外部守护器故障转移。"""
    stopped_tasks = _stopped_critical_background_tasks(request.app)
    worker_heartbeat = _worker_heartbeat_health(request.app)
    broker = getattr(request.app.state, "job_broker", None)
    expired_claim_snapshot = {
        "healthy": True,
        "expired_count": 0,
        "sample": [],
    }
    inspect_expired_claims = getattr(
        broker,
        "expired_claim_health_snapshot",
        None,
    )
    if callable(inspect_expired_claims):
        try:
            expired_claim_snapshot = inspect_expired_claims()
        except sqlite3.OperationalError:
            # Writer contention is already surfaced by scheduler telemetry.
            # It must not turn a transient health read into a restart loop.
            logger.warning(
                "Unable to inspect expired job claims due to SQLite contention"
            )
            expired_claim_snapshot["inspection"] = "sqlite_contention"
    unhealthy_tasks = (
        []
        if worker_heartbeat["healthy"] or "job_worker" in stopped_tasks
        else ["job_worker_heartbeat"]
    )
    if not expired_claim_snapshot["healthy"]:
        unhealthy_tasks.append("expired_job_claims")
    healthy = not stopped_tasks and not unhealthy_tasks
    resource_budget = (
        broker.worker_health_snapshot()
        if broker is not None
        else {
            "online": False,
            "admission_mode": "starting",
            "pause_heavy": True,
            "reasons": ["starting"],
        }
    )
    if not healthy:
        response.status_code = 503
    code_evidence = runtime_code_evidence()
    return {
        "status": "ok" if healthy else "degraded",
        "version": APP_VERSION,
        "commit": APP_COMMIT,
        "code_version": code_evidence["code_version"],
        "code_identity": code_evidence["identity"],
        "observed_worktree_drift": code_evidence[
            "observed_worktree_drift"
        ],
        "started_at": RUNTIME_STARTED_AT,
        "critical_processes": {
            "healthy": healthy,
            "stopped": stopped_tasks,
            "unhealthy": unhealthy_tasks,
            "job_worker_heartbeat": worker_heartbeat,
        },
        "job_claim_leases": expired_claim_snapshot,
        # Pressure is an admission signal, not a restart signal: the watchdog
        # can observe it without restarting an otherwise healthy API.
        "resource_budget": {
            "admission_mode": resource_budget.get("admission_mode"),
            "pause_heavy": resource_budget.get("pause_heavy"),
            "capacity": resource_budget.get("desired_capacity"),
            "running_slots": resource_budget.get("running_slots"),
            "reasons": resource_budget.get("reasons"),
            "metrics": resource_budget.get("metrics"),
        },
    }
