"""应用配置 —— 使用 pydantic-settings 管理所有运行参数."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """量化验证平台全局配置。

    所有路径均相对于项目根目录 (PROJECT_ROOT) 解析。
    """

    # ── 路径 ──────────────────────────────────────────────
    DATABASE_DIR: str = "data"
    USERS_DB: str = "data/users.db"
    EXPERIMENT_DB: str = "data/experiment.db"
    TRADING_SIM_DB: str = "data/trading_sim.db"
    TRADING_LIVE_DB: str = "data/trading_live.db"
    DATA_CACHE_DIR: str = "data/cache"
    DATA_STAGING_DIR: str = "data/staging"
    PIT_EVIDENCE_DIR: str = "data/pit_evidence"
    PIT_EVIDENCE_DB: str = "data/pit_evidence/governance.db"
    # Append-only metadata for provider licence/archive receipts. The registry
    # stores digests and redacted reference fingerprints only; it is purposely
    # separate from both governance data and the production release registry.
    PIT_LICENCE_EVIDENCE_DB: str = "data/pit_evidence/licence_evidence.db"
    # JSON object keyed by calendar signing-key ID. Each value contains the
    # Ed25519 public key (base64), exact provider and authorized evidence level.
    # Empty by default: production PIT history remains fail-closed until a
    # governed trust anchor is provisioned outside the repository.
    PIT_CALENDAR_TRUSTED_KEYS_JSON: str = "{}"
    # Explicit, non-production E2E fixture boundary.  Empty by default and
    # ignored outside ENVIRONMENT=test.  The verifier additionally requires
    # every mutable runtime path to live below this isolated root.
    PIT_QA_FIXTURE_ROOT: str = ""
    PIT_QA_ATTESTATION: str = ""
    MODEL_STORE_DIR: str = "data/models"
    RESEARCH_SNAPSHOT_DIR: str = "data/research_snapshots"
    # Versioned, explicitly non-production data generations.  These files may
    # be used by exploratory research and paper simulation with visible risk
    # warnings, but never satisfy a live-trading release gate.
    RESEARCH_DATA_DIR: str = "data/research_data"

    # ── DeepSeek AI ────────────────────────────────────────
    DEEPSEEK_API_KEY: str = ""
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com"

    # ── JWT ───────────────────────────────────────────────
    ENVIRONMENT: str = "development"
    JWT_SECRET: str = "change-me-in-production"
    JWT_EXPIRE_MINUTES: int = 1440
    BOOTSTRAP_ADMIN_TOKEN: str = ""
    # Browser/API authentication controls.  These intentionally have modest
    # defaults for a single-user installation; a reverse proxy must remain the
    # outer DoS boundary.  The application limiter is process-local and is
    # therefore a credential-stuffing brake, not a distributed WAF.
    AUTH_LOGIN_MAX_ATTEMPTS: int = 8
    AUTH_LOGIN_WINDOW_SECONDS: int = 300
    AUTH_REFRESH_MAX_ATTEMPTS: int = 30
    AUTH_REFRESH_WINDOW_SECONDS: int = 300
    AUTH_SENSITIVE_MAX_ATTEMPTS: int = 60
    AUTH_SENSITIVE_WINDOW_SECONDS: int = 60
    AUTH_SESSION_MAX_DAYS: int = 7

    # ── CORS ──────────────────────────────────────────────
    CORS_ORIGINS: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]

    # Paper-trading end-of-day cycle. The scheduler only submits work; the
    # durable job broker and idempotency key make restarts safe.
    PAPER_SIMULATION_AUTO_RUN: bool = True
    PAPER_SIMULATION_RUN_TIME: str = "18:05"
    PAPER_SIMULATION_TIMEZONE: str = "Asia/Shanghai"
    PAPER_SIMULATION_REFRESH_DATA: bool = True
    SIMULATION_RUN_LEASE_SECONDS: int = 7200
    # Explicit governance actor for unattended evidence collection. Zero keeps
    # automation fail-closed; never invent or impersonate an administrator.
    PIT_AUTOMATION_ACTOR_USER_ID: int = 0
    # Dedicated non-admin identity for the independent durable PIT updater.
    # Both fields must match an active users.db row with data:update permission.
    PIT_AUTOMATION_SERVICE_USER_ID: int = 0
    PIT_AUTOMATION_SERVICE_USERNAME: str = "pit_automation"
    PIT_AUTOMATION_AUTO_RUN: bool = False
    PIT_AUTOMATION_SCAN_MINUTES: int = 30
    PIT_AUTOMATION_LEASE_SECONDS: int = 120
    # A remote source or importer must never retain its PIT lease forever.
    # The state machine cancels the child task and schedules the same stage
    # for retry when this bounded deadline expires.
    PIT_AUTOMATION_STAGE_TIMEOUT_SECONDS: int = 300
    PIT_AUTOMATION_RETRY_BASE_SECONDS: int = 30
    PIT_AUTOMATION_PERSONAL_MODE: bool = False
    PIT_AUTOMATION_AUTO_ACTIVATE_GREEN: bool = False

    # Candidate-provider probes are a separate quarantine-only boundary. The
    # scheduler persists only public request scope and evidence digests; the
    # secret is resolved by the executing worker and SecretStr keeps accidental
    # Settings repr/model dumps redacted.
    TUSHARE_TOKEN: SecretStr = SecretStr("")
    # Optional explicit proxy for quarantine-only provider probes. macOS
    # LaunchDaemons do not inherit the interactive user's System Settings
    # proxy environment, so relying on HTTP_PROXY makes unattended collection
    # nondeterministic. Keep the complete URL secret because it may contain
    # proxy credentials; the adapter accepts only a loopback HTTP(S) proxy and
    # never writes this value into jobs, artifacts, reports, or logs.
    PIT_CANDIDATE_OUTBOUND_PROXY_URL: SecretStr = SecretStr("")
    PIT_CANDIDATE_PREFLIGHT_AUTO_RUN: bool = False
    PIT_CANDIDATE_PREFLIGHT_SCAN_MINUTES: int = 360
    PIT_CANDIDATE_PREFLIGHT_LOOKBACK_DAYS: int = 14
    PIT_CANDIDATE_PREFLIGHT_TS_CODE: str = "000001.SZ"
    PIT_CANDIDATE_PREFLIGHT_INDEX_CODE: str = "000300.SH"
    PIT_CANDIDATE_PREFLIGHT_CROSS_CHECK: bool = True

    # Independent personal-research refresh. This is not the strict
    # production data updater and does not depend on an active paper portfolio.
    # A six-hour scan enqueues at most one durable Tushare job per local date.
    RESEARCH_DATA_REFRESH_AUTO_RUN: bool = True
    RESEARCH_DATA_REFRESH_SCAN_MINUTES: int = 360
    RESEARCH_DATA_REFRESH_FROM_MONTH: str = "2016-01"
    RESEARCH_DATA_REFRESH_MAX_CALLS: int = 16
    RESEARCH_DATA_REFRESH_DAILY_MAX_ATTEMPTS: int = 3
    RESEARCH_DATA_REFRESH_RETRY_COOLDOWN_MINUTES: int = 60

    # Periodic model maintenance. The scheduler only submits durable retrain
    # jobs; candidate validation and atomic champion promotion remain in the
    # normal job execution path.
    MODEL_RETRAIN_AUTO_RUN: bool = True
    MODEL_RETRAIN_SCAN_MINUTES: int = 30
    MODEL_RETRAIN_FAILURE_RETRY_HOURS: int = 24

    # ── 本机资源感知任务调度 ──────────────────────────────
    # 8 GB Apple Silicon 的保守默认值。调度硬上限固定为 2；CPU 密集型
    # 研究另由单并发、可回收的 spawn 子进程执行，避免拖死 API 进程。
    JOB_SCHEDULER_ENABLED: bool = True
    JOB_SCHEDULER_MAX_CONCURRENCY: int = 2
    JOB_SCHEDULER_CPU_LOAD_LIMIT: float = 0.70
    JOB_SCHEDULER_MEMORY_USED_LIMIT: float = 0.82
    JOB_SCHEDULER_MIN_AVAILABLE_MEMORY_MB: int = 1536
    JOB_SCHEDULER_MAX_SWAP_USED_MB: int = 1536
    JOB_SCHEDULER_MAX_SWAP_GROWTH_MB: int = 128
    JOB_SCHEDULER_SCALE_UP_SAMPLES: int = 3
    JOB_SCHEDULER_SAMPLE_SECONDS: float = 5.0
    JOB_SCHEDULER_POLL_SECONDS: float = 1.0
    JOB_SCHEDULER_LEASE_SECONDS: int = 45
    JOB_SCHEDULER_SWEEP_MAX_RUNNING: int = 1
    JOB_SCHEDULER_MAX_PENDING_JOBS: int = 500
    JOB_SCHEDULER_AGING_SECONDS: int = 300
    JOB_SCHEDULER_LIGHT_BACKTEST_MAX_CODES: int = 50
    # One account cannot fill the durable queue and starve every other user.
    # Scheduled system work (user_id=NULL) remains governed by the global cap.
    JOB_SCHEDULER_MAX_ACTIVE_PER_USER: int = 100
    # Hard admission budgets.  These are intentionally stricter than the
    # scale-up thresholds: crossing one pauses *new* heavy work while the API
    # and already-running jobs remain alive and cancellable.
    JOB_SCHEDULER_CRITICAL_CPU_LOAD: float = 1.10
    JOB_SCHEDULER_CRITICAL_MEMORY_USED: float = 0.92
    JOB_SCHEDULER_CRITICAL_AVAILABLE_MEMORY_MB: int = 768
    JOB_SCHEDULER_MIN_DISK_FREE_MB: int = 2048
    JOB_SCHEDULER_MAX_IO_PRESSURE: float = 0.80
    # Native ML libraries must not silently turn one broker slot into an
    # unbounded process pool.  One worker is the safe default on an 8 GB host.
    JOB_CPU_THREAD_BUDGET: int = 1
    JOB_ISOLATED_CPU_TIMEOUT_SECONDS: int = 1800
    # One child may reserve at most half of the supported 8 GB host on Linux.
    # macOS/Windows retain the single-slot admission boundary until a native
    # memory controller is implemented; this is documented, never implied.
    JOB_ISOLATED_CPU_MEMORY_LIMIT_MB: int = 4096
    JOB_OBSERVABILITY_RETENTION_HOURS: int = 168
    JOB_SLO_WINDOW_HOURS: int = 24
    JOB_SLO_EVALUATION_SECONDS: int = 60
    JOB_SLO_CONFIRMATIONS_REQUIRED: int = 2
    JOB_SLO_ALERT_COOLDOWN_SECONDS: int = 900
    # External SLO delivery is opt-in.  A valid public HTTPS endpoint and a
    # 16+ character signing secret are required before the outbox performs a
    # network request; disabled is the safe default for personal deployments.
    ALERT_WEBHOOK_ENABLED: bool = False
    ALERT_WEBHOOK_URL: str = ""
    ALERT_WEBHOOK_SIGNING_SECRET: SecretStr = SecretStr("")
    ALERT_WEBHOOK_TIMEOUT_SECONDS: int = 5
    ALERT_WEBHOOK_MAX_ATTEMPTS: int = 5
    ALERT_WEBHOOK_RETRY_BASE_SECONDS: int = 60
    ALERT_WEBHOOK_BATCH_SIZE: int = 10
    ALERT_WEBHOOK_ACK_ESCALATION_SECONDS: int = 3600

    # ── 内部常量（不在环境变量中覆盖）─────────────────────
    PROJECT_ROOT: ClassVar[Path] = Path(__file__).resolve().parent.parent

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    def abs_path(self, relative: str) -> Path:
        """将相对路径转为基于项目根的绝对路径。"""
        return self.PROJECT_ROOT / relative


# 全局单例
settings = Settings()
