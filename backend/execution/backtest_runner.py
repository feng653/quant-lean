"""回测执行器（原 main._run_experiment，v0.4.0 抽层）。

一次可复现回测的完整执行与原子落库：加载行情/股票池 → PIT 门禁 →
策略信号 → 回测引擎 → 指标/净值/成交持久化 → 实验清单固化。
CPU 密集策略工作通过 ``loop.run_in_executor`` 执行；任务进度与取消
通过显式回调注入（原闭包 _cpu_work/_wf_progress/_wf_cancelled）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import traceback
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

import aiosqlite
import pandas as pd

from backend.config import settings
from backend.core.cost_model import CostModel
from backend.core.engine import BacktestEngine, ExecutionConstraints
from backend.core.metrics import compute_all_metrics
from backend.core.security_boundaries import sanitize_diagnostic
from backend.data.cache import (
    DataCache,
    has_price_field,
    resolve_pool_benchmark,
)
from backend.data import cache_readiness
from backend.data.lineage import (
    STATIC_UNIVERSE,
    UniverseSnapshot,
    build_universe_snapshot,
)
from backend.data.market_quality import audit_market_data
from backend.data import pit_runtime
from backend.data.point_in_time_master import PointInTimeMasterStore
from backend.data.point_in_time_universe import (
    filter_timeline_by_industry,
    filter_timeline_codes,
    resolve_point_in_time_universe,
    select_market_data_for_timeline,
    timeline_from_identity,
    validate_signals_against_timeline,
)
from backend.data.research_snapshots import (
    ResearchSnapshotStore,
    clip_to_test_end,
)
from backend.data.sources.validated import build_public_research_source
from backend.data.universe import (
    POOL_NAME_ALIASES,
    PRESET_POOLS,
    UniverseManager,
)
from backend.data.versioning import compute_dataset_version
from backend.jobs import broker as broker_module
from backend.jobs.broker import JobCancelledError
from backend.services.ml_promotion_evidence import (
    build_model_promotion_evidence,
    finite_json_value,
)
from backend.services.model_serialization import contract_for_model
from backend.services.research_manifest import (
    ManifestDriftError,
    build_run_manifest,
    expected_replay_differences,
    load_run_manifest,
    persist_initial_manifest,
    resolve_execution_payload,
)
from backend.services.research_runtime import (
    build_research_trust,
    load_research_benchmark,
    load_research_market,
    normalize_research_provenance,
    verify_research_runtime_binding,
)
from backend.services.walkforward import (
    WalkForwardCancelled,
    run_walk_forward,
)
from backend.strategies.base import (
    TrainableStrategy,
    split_platform_params,
)
from backend.strategies.ml.runtime import preload_strategy_native_runtime
from backend.strategies.registry import get_registry
from backend.strategies.research_context import (
    StrategyResearchContext,
    activate_research_context,
    validate_strategy_research_context,
)

logger = logging.getLogger("quant_platform")


def _parse_list(value: object) -> list[str]:
    """Parse a JSON array or comma-separated database field."""
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


def _make_wf_progress_callback(
    broker: Any,
    job_uuid: str,
    loop: asyncio.AbstractEventLoop,
) -> Callable[[float, str], None]:
    """显式进度回调：把 walk-forward 进度折算到 15%~70% 段写回 broker。"""

    def _wf_progress(frac: float, msg: str) -> None:
        try:
            asyncio.run_coroutine_threadsafe(
                broker.update_job_progress(
                    job_uuid,
                    progress=0.15 + 0.55 * max(0.0, min(frac, 1.0)),
                    message=msg,
                    stage="backtesting",
                ),
                loop,
            )
        except Exception:
            pass  # progress failure does not affect execution

    return _wf_progress


def _make_wf_cancelled_callback(
    broker: Any,
    job_uuid: str,
    loop: asyncio.AbstractEventLoop,
) -> Callable[[], bool]:
    """显式取消回调：询问 broker 当前任务是否已被请求取消。"""

    def _wf_cancelled() -> bool:
        try:
            return bool(
                asyncio.run_coroutine_threadsafe(
                    broker.is_cancel_requested(job_uuid), loop
                ).result(timeout=10)
            )
        except Exception:
            return False

    return _wf_cancelled


def _cpu_work(
    strategy: Any,
    pivot: pd.DataFrame,
    runtime_params: dict[str, Any],
    exp: Any,
    strategy_meta: Any,
    point_in_time_timeline: Any,
    execution_manifest: dict[str, Any],
    benchmark_close: pd.Series,
    params: dict[str, Any],
    exp_id: int,
    data_version: str,
    manifest_hash: str,
    wf_progress: Callable[[float, str], None],
    wf_cancelled: Callable[[], bool],
) -> tuple[Any, dict[str, Any], Any]:
    """CPU 密集段：策略信号 → 回测引擎 → 指标/模型工件。

    由 ``run_experiment`` 在事件循环线程里显式创建回调（进度/取消）并
    作为参数传入，避免在 CPU executor 中闭包捕获 event loop 与 broker。
    """
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
            try:
                wf_result = run_walk_forward(
                    strategy,
                    pivot,
                    runtime_params,
                    exp["test_start"],
                    exp["test_end"],
                    progress_callback=wf_progress,
                    cancel_callback=wf_cancelled,
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
        validate_signals_against_timeline(
            signals,
            point_in_time_timeline,
        )
    if strategy_meta.requires_training and not any(signals.values()):
        raise RuntimeError("训练型策略未生成任何信号；请检查训练窗口和训练日志")

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

async def run_experiment(exp_id: int, job_uuid: str) -> None:
    """Execute and atomically persist one reproducible backtest run."""

    broker = broker_module.get_broker()
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

        pool_id = pit_runtime.require_pit_pool(pool_id)
        if data_access_policy != "cache_only":
            raise pit_runtime.PitRuntimeDataError(
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
                raise pit_runtime.PitRuntimeDataError(
                    "pit_replay_evidence_missing",
                    "精确重跑来源未绑定 PIT-only 运行证明，禁止重放旧静态快照",
                )
        elif research_trust_profile == "tushare_research_trusted":
            # Submission binds an immutable ResearchDataStore generation. The
            # worker loads that exact generation below; it must not rebuild a
            # separate legacy evidence timeline or drift to the active pointer.
            pass
        else:
            try:
                await pit_runtime.require_pit_runtime_input(
                    pool_id=pool_id,
                    required_start=calculation_start,
                    required_end=exp["test_end"],
                    purpose="research",
                )
            except pit_runtime.PitRuntimeDataError:
                # 测试分支放宽（v0.8.x 分级门禁，与提交端 _require_pit_submission 一致）：
                # 研究/模拟用途在 PIT 数据未激活时降级放行，使用可用缓存数据运行；
                # 结果仅供研究参考。风险由研究清单与后续数据校验兜底。
                import logging

                logging.getLogger("quant_platform").warning(
                    "PIT runtime input unavailable for experiment %s; "
                    "degraded to cached-data research run",
                    exp["experiment_id"],
                )

        cache = DataCache()
        source = None
        if data_access_policy == "allow_fetch":

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
                custom_key = cache_readiness.custom_cache_key(selected_codes)
                runtime_cache_key = custom_key
                required_start = exp["train_start"] or (
                    exp["test_start"]
                    if data_access_policy == "cache_only"
                    else "2015-01-01"
                )
                required_end = exp["test_end"]
                if data_access_policy == "cache_only":
                    inspected = await cache_readiness.require_cached_market_data(
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
                inspected = await cache_readiness.require_cached_market_data(
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

            pit_store = PointInTimeMasterStore()
            if research_trust_profile == "tushare_research_trusted":

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
                    inspected_benchmark = await cache_readiness.require_cached_benchmark(
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

        registry = get_registry()
        strategy = registry.create_strategy(exp["strategy_id"])
        strategy_meta = registry.get_metadata(exp["strategy_id"])
        params = json.loads(exp["params"])

        strategy_params, _ = split_platform_params(params)
        is_valid, validation_error = strategy.validate_params(strategy_params)
        if not is_valid:
            raise ValueError(f"策略参数无效: {validation_error}")
        runtime_params = dict(strategy_params)
        if exp["train_start"]:
            runtime_params["_train_start"] = exp["train_start"]
        if exp["train_end"]:
            runtime_params["_train_end"] = exp["train_end"]

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

        # Native ML libraries must be loaded on this event-loop thread before
        # the CPU-bound strategy work enters the executor. In particular,
        # first importing LightGBM from the executor can segfault on macOS.

        # Current trainable strategies are blocked until platform-owned PIT
        # sample and label masks exist; do not load native runtimes first.
        if not isinstance(strategy, TrainableStrategy):
            preload_strategy_native_runtime(exp["strategy_id"])
        loop = asyncio.get_running_loop()
        wf_progress = _make_wf_progress_callback(broker, job_uuid, loop)
        wf_cancelled = _make_wf_cancelled_callback(broker, job_uuid, loop)

        result, metrics, artifact = await loop.run_in_executor(
            None,
            _cpu_work,
            strategy,
            pivot,
            runtime_params,
            exp,
            strategy_meta,
            point_in_time_timeline,
            execution_manifest,
            benchmark_close,
            params,
            exp_id,
            data_version,
            manifest_hash,
            wf_progress,
            wf_cancelled,
        )
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

