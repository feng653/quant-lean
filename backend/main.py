"""FastAPI 应用入口 —— 量化验证平台 V3（v0.4.0 抽层后仅保留装配层）。"""

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
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from backend.config import settings
from backend.core.log_redaction import install_sensitive_url_log_filter
from backend.core.request_id import attach_request_id
from backend.db.init import init_databases
from backend.jobs.broker import get_broker
from backend.jobs.observability import structured_log
from backend.jobs.worker import (
    shutdown_background_runtime,
    stopped_critical_background_tasks,
    supervise_job_worker,
    worker_heartbeat_health,
)
from backend.services.candidate_preflight_scheduler import (
    run_candidate_preflight_scheduler,
)
from backend.services.model_lifecycle import run_model_retrain_scheduler
from backend.services.pit_automation_scheduler import (
    run_pit_automation_scheduler,
)
from backend.services.research_data_scheduler import (
    run_research_data_scheduler,
)
from backend.services.simulation_scheduler import (
    run_paper_simulation_scheduler,
)
from backend.strategies.startup import scan_strategies

install_sensitive_url_log_filter()
logger = logging.getLogger("quant_platform")


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

    await init_databases()
    await scan_strategies()

    # Start background workers. The scheduler only enqueues durable jobs, so
    # the execution path is identical for manual and automatic simulations.
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
        supervise_job_worker(),
        name="job-worker-supervisor",
    )
    scheduler_task = asyncio.create_task(run_paper_simulation_scheduler())
    model_scheduler_task = asyncio.create_task(
        run_model_retrain_scheduler(),
        name="model-retrain-scheduler",
    )
    pit_scheduler_task = asyncio.create_task(
        run_pit_automation_scheduler(),
        name="pit-automation-scheduler",
    )
    candidate_preflight_scheduler_task = asyncio.create_task(
        run_candidate_preflight_scheduler(),
        name="candidate-preflight-scheduler",
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
        await shutdown_background_runtime(
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
    stopped_tasks = stopped_critical_background_tasks(request.app)
    worker_heartbeat = worker_heartbeat_health(request.app)
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
