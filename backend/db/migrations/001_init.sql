-- ============================================================================
-- 量化验证平台 V3 — 统一数据库初始化脚本
-- 合并 users / experiment / trading_sim 三库的完整 DDL
-- ============================================================================

-- ═══════════════════════════════════════════════════════════════════════════
-- 1. users.db — 用户与权限
-- ═══════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT    UNIQUE NOT NULL,
    password_hash   TEXT    NOT NULL,
    display_name    TEXT,
    email           TEXT,
    is_admin        INTEGER DEFAULT 0,      -- 首位注册用户 = 1，其余 = 0
    is_active       INTEGER DEFAULT 1,
    created_at      TEXT    DEFAULT (datetime('now')),
    last_login      TEXT
);

CREATE TABLE IF NOT EXISTS user_permissions (
    user_id     INTEGER NOT NULL,
    permission  TEXT    NOT NULL,            -- e.g. "experiments:create"
    granted_by  INTEGER,                    -- 授权人 ID
    granted_at  TEXT    DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, permission),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS user_sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    token_jti   TEXT    UNIQUE NOT NULL,     -- JWT jti claim (用于撤销)
    expires_at  TEXT    NOT NULL,
    created_at  TEXT    DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
CREATE INDEX IF NOT EXISTS idx_user_sessions_token ON user_sessions(token_jti);

-- Stateful browser/device sessions.  Refresh bearer tokens are never stored:
-- only a SHA-256 proof is retained, and each proof is single-use.
CREATE TABLE IF NOT EXISTS auth_sessions (
    session_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    family_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    revoke_reason TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS auth_refresh_tokens (
    token_jti TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL,
    family_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    revoked_at TEXT,
    FOREIGN KEY (session_id) REFERENCES auth_sessions(session_id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

-- ═══════════════════════════════════════════════════════════════════════════
-- 2. experiment.db — 实验管理
-- ═══════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS experiments (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             INTEGER NOT NULL,       -- 创建者（审计用）

    name                TEXT,
    strategy_id         TEXT    NOT NULL,
    strategy_category   TEXT    NOT NULL,        -- technical|ml|factor|portfolio|composite

    -- 标注（V3 新增）
    is_starred          INTEGER DEFAULT 0,
    labels              TEXT,                    -- JSON 数组 ["表现最佳","低回撤"]

    -- 股票池
    pool_preset         TEXT,
    pool_custom_codes   TEXT,
    pool_industries     TEXT,

    -- 时间窗口
    train_start         TEXT,
    train_end           TEXT,
    test_start          TEXT    NOT NULL,
    test_end            TEXT    NOT NULL,

    -- 参数
    params              TEXT    NOT NULL,        -- JSON
    params_hash         TEXT    NOT NULL,
    mode                TEXT    DEFAULT 'batch',

    -- 训练相关（V3 新增）
    requires_training   INTEGER DEFAULT 0,
    retrain_frequency   TEXT,                    -- never|daily|weekly|monthly|quarterly

    -- 状态
    status              TEXT    DEFAULT 'pending',  -- pending|running|completed|failed
    error_log           TEXT,
    ai_diagnosis        TEXT,

    progress_pct        REAL    DEFAULT 0,
    progress_message    TEXT,

    data_version        TEXT,
    code_version        TEXT,

    created_at          TEXT    DEFAULT (datetime('now')),
    started_at          TEXT,
    completed_at        TEXT
);

CREATE TABLE IF NOT EXISTS experiment_metrics (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id   INTEGER NOT NULL,
    metric_name     TEXT    NOT NULL,             -- e.g. "sharpe_ratio"
    metric_value    REAL,
    metric_json     TEXT,                         -- 复杂指标存储 JSON
    period          TEXT    DEFAULT 'full',       -- full|train|test|year_2024
    created_at      TEXT    DEFAULT (datetime('now')),
    FOREIGN KEY (experiment_id) REFERENCES experiments(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS equity_curve (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id   INTEGER NOT NULL,
    date            TEXT    NOT NULL,
    equity          REAL    NOT NULL,
    benchmark       REAL,
    cash            REAL,
    daily_return    REAL,
    drawdown        REAL,
    FOREIGN KEY (experiment_id) REFERENCES experiments(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS trade_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id   INTEGER NOT NULL,
    date            TEXT    NOT NULL,
    code            TEXT    NOT NULL,
    action          TEXT    NOT NULL,            -- BUY|SELL
    price           REAL    NOT NULL,
    shares          INTEGER NOT NULL,
    amount          REAL    NOT NULL,
    cost            REAL    DEFAULT 0.0,
    signal_strategy TEXT,
    signal_score    REAL    DEFAULT 0.0,
    FOREIGN KEY (experiment_id) REFERENCES experiments(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS model_artifacts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id       INTEGER NOT NULL,
    strategy_id         TEXT    NOT NULL,
    model_version       INTEGER DEFAULT 1,       -- V3 新增
    model_file_path     TEXT    NOT NULL,
    metadata_file_path  TEXT    NOT NULL,
    params_hash         TEXT    NOT NULL,
    train_window_start  TEXT,
    train_window_end    TEXT,
    feature_count       INTEGER,
    train_samples       INTEGER,
    train_metrics       TEXT,                    -- JSON
    feature_importance  TEXT,                    -- JSON
    is_latest           INTEGER DEFAULT 1,       -- V3 新增
    created_at          TEXT    DEFAULT (datetime('now')),
    FOREIGN KEY (experiment_id) REFERENCES experiments(id) ON DELETE CASCADE
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_exp_strategy ON experiments(strategy_id);
CREATE INDEX IF NOT EXISTS idx_exp_status ON experiments(status);
CREATE INDEX IF NOT EXISTS idx_exp_user ON experiments(user_id);
CREATE INDEX IF NOT EXISTS idx_exp_created ON experiments(created_at);
CREATE INDEX IF NOT EXISTS idx_metrics_exp ON experiment_metrics(experiment_id);
CREATE INDEX IF NOT EXISTS idx_equity_exp ON equity_curve(experiment_id);
CREATE INDEX IF NOT EXISTS idx_equity_date ON equity_curve(date);
CREATE INDEX IF NOT EXISTS idx_trade_exp ON trade_log(experiment_id);
CREATE INDEX IF NOT EXISTS idx_model_exp ON model_artifacts(experiment_id);
CREATE INDEX IF NOT EXISTS idx_model_latest ON model_artifacts(strategy_id, is_latest);

-- ═══════════════════════════════════════════════════════════════════════════
-- 3. trading_sim.db — 模拟交易（trading_live.db 结构相同）
-- ═══════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS deployments (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id                 INTEGER NOT NULL,

    strategy_id             TEXT    NOT NULL,
    strategy_category       TEXT    NOT NULL,     -- V3 新增
    display_name            TEXT,
    params                  TEXT    NOT NULL,     -- JSON
    params_hash             TEXT    NOT NULL,
    mode                    TEXT    DEFAULT 'batch',

    source_experiment_id    INTEGER,             -- 来源实验
    source_model_artifact_id INTEGER,            -- V3 新增: 具体模型版本

    -- 重训练配置（V3 新增）
    requires_retraining     INTEGER DEFAULT 0,
    retrain_frequency       TEXT,                -- never|daily|weekly|monthly|quarterly
    last_retrain_at         TEXT,
    current_model_version   INTEGER DEFAULT 1,
    current_model_path      TEXT,

    position_mode           TEXT    DEFAULT 'equal_weight',
    position_config         TEXT,                -- JSON

    status                  TEXT    DEFAULT 'active',
    status_tags             TEXT,                -- JSON 数组
    user_notes              TEXT,

    deployed_at             TEXT    DEFAULT (datetime('now')),
    last_signal_at          TEXT,
    last_rebalance_at       TEXT,
    stopped_at              TEXT,

    created_at              TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS portfolios (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    deployment_id   INTEGER NOT NULL,
    name            TEXT,
    initial_capital REAL    NOT NULL,
    current_cash    REAL    NOT NULL,
    status          TEXT    DEFAULT 'active',
    created_at      TEXT    DEFAULT (datetime('now')),
    FOREIGN KEY (deployment_id) REFERENCES deployments(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS daily_signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    deployment_id   INTEGER NOT NULL,
    date            TEXT    NOT NULL,
    code            TEXT    NOT NULL,
    action          TEXT    NOT NULL,             -- BUY|SELL|HOLD
    score           REAL    DEFAULT 0.0,
    weight          REAL    DEFAULT 0.0,
    confidence      REAL    DEFAULT 0.0,
    reasoning       TEXT,
    created_at      TEXT    DEFAULT (datetime('now')),
    FOREIGN KEY (deployment_id) REFERENCES deployments(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS position_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    deployment_id   INTEGER NOT NULL,
    date            TEXT    NOT NULL,
    code            TEXT    NOT NULL,
    shares          INTEGER NOT NULL,
    avg_cost        REAL    NOT NULL,
    close_price     REAL,
    market_value    REAL,
    unrealized_pnl  REAL    DEFAULT 0.0,
    snapshot_at     TEXT    DEFAULT (datetime('now')),
    FOREIGN KEY (deployment_id) REFERENCES deployments(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    deployment_id   INTEGER NOT NULL,
    date            TEXT    NOT NULL,
    code            TEXT    NOT NULL,
    action          TEXT    NOT NULL,             -- BUY|SELL
    price           REAL    NOT NULL,
    shares          INTEGER NOT NULL,
    amount          REAL    NOT NULL,
    cost            REAL    DEFAULT 0.0,
    signal_strategy TEXT,
    signal_score    REAL    DEFAULT 0.0,
    order_type      TEXT    DEFAULT 'market',     -- market|limit
    status          TEXT    DEFAULT 'pending',    -- pending|filled|rejected|cancelled
    reject_reason   TEXT,
    filled_at       TEXT,
    created_at      TEXT    DEFAULT (datetime('now')),
    FOREIGN KEY (deployment_id) REFERENCES deployments(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS nav_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    deployment_id   INTEGER NOT NULL,
    portfolio_id    INTEGER,
    date            TEXT    NOT NULL,
    nav             REAL    NOT NULL,
    daily_return    REAL,
    total_equity    REAL,
    cash            REAL,
    market_value    REAL,
    created_at      TEXT    DEFAULT (datetime('now')),
    FOREIGN KEY (deployment_id) REFERENCES deployments(id) ON DELETE CASCADE,
    FOREIGN KEY (portfolio_id) REFERENCES portfolios(id) ON DELETE SET NULL
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_dep_strategy ON deployments(strategy_id);
CREATE INDEX IF NOT EXISTS idx_dep_status ON deployments(status);
CREATE INDEX IF NOT EXISTS idx_dep_user ON deployments(user_id);
CREATE INDEX IF NOT EXISTS idx_portfolio_dep ON portfolios(deployment_id);
CREATE INDEX IF NOT EXISTS idx_signal_dep_date ON daily_signals(deployment_id, date);
CREATE INDEX IF NOT EXISTS idx_position_dep_date ON position_snapshots(deployment_id, date);
CREATE INDEX IF NOT EXISTS idx_order_dep_date ON orders(deployment_id, date);
CREATE INDEX IF NOT EXISTS idx_nav_dep_date ON nav_history(deployment_id, date);

-- ═══════════════════════════════════════════════════════════════════════════
-- 4. jobs 表（跨库共用，存于各库或独立文件均可；此处定义于 experiment.db）
-- ═══════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS jobs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    job_type            TEXT NOT NULL,
    display_name        TEXT,
    params              TEXT,
    status              TEXT DEFAULT 'pending',
    progress            REAL DEFAULT 0.0,
    progress_message    TEXT,
    current_stage       TEXT,
    result              TEXT,
    error               TEXT,
    resource_type       TEXT,
    resource_id         TEXT,
    parent_job_uuid     TEXT,
    attempt             INTEGER DEFAULT 1,
    worker_id           TEXT,
    cancel_requested_at TEXT,
    heartbeat_at        TEXT,
    created_at          TEXT DEFAULT (datetime('now')),
    updated_at          TEXT DEFAULT (datetime('now')),
    started_at          TEXT,
    completed_at        TEXT,
    user_id             INTEGER,
    job_uuid            TEXT UNIQUE NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_type ON jobs(job_type);
CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs(user_id);

CREATE TABLE IF NOT EXISTS job_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_uuid    TEXT NOT NULL,
    status      TEXT,
    progress    REAL,
    stage       TEXT,
    message     TEXT,
    created_at  TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_job_events_job ON job_events(job_uuid, id DESC);

-- ═══════════════════════════════════════════════════════════════════════════
-- 5. AI 调用缓存与用量审计（experiment.db）
-- ═══════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS ai_cache (
    cache_key       TEXT PRIMARY KEY,
    result_json     TEXT NOT NULL,
    expires_at      REAL NOT NULL,
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_ai_cache_expires ON ai_cache(expires_at);

CREATE TABLE IF NOT EXISTS ai_usage (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint            TEXT NOT NULL,
    user_id             INTEGER,
    cache_key           TEXT NOT NULL,
    model               TEXT,
    prompt_tokens       INTEGER NOT NULL DEFAULT 0,
    completion_tokens   INTEGER NOT NULL DEFAULT 0,
    total_tokens        INTEGER NOT NULL DEFAULT 0,
    latency_ms          REAL NOT NULL DEFAULT 0,
    cache_hit           INTEGER NOT NULL DEFAULT 0,
    success             INTEGER NOT NULL DEFAULT 1,
    error_type          TEXT,
    created_at          TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_ai_usage_user_created
ON ai_usage(user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_ai_usage_endpoint_created
ON ai_usage(endpoint, created_at DESC);
