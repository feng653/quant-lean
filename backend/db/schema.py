"""SQLite schema definitions (moved out of main.py during the v0.4.0 抽层).

只读常量：数据库建表 SQL 的唯一事实源。迁移逻辑见 backend/db/migrate.py，
启动建库/迁移入口见 backend/db/init.py。
"""

USERS_SCHEMA = """
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

EXPERIMENT_SCHEMA = """
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

TRADING_SIM_SCHEMA = """
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
