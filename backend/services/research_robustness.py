"""Read-only robustness reports over persisted experiment evidence.

The report is deliberately a post-hoc diagnostic. It never persists evidence,
changes a workflow state, queries market data, or treats a test-set diagnostic
as selection or promotion evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
import math
from typing import Any, Mapping

import aiosqlite
import numpy as np
import pandas as pd

from backend.research.robustness import (
    block_bootstrap_performance,
    cscv_probability_of_backtest_overfitting,
    deflated_sharpe_ratio,
    probabilistic_sharpe_ratio,
)
from backend.services.research_manifest import (
    RUN_MANIFEST_SCHEMA,
    canonical_sha256,
)


REPORT_SCHEMA_VERSION = "research-robustness-report/v1"
_MIN_RETURN_SAMPLES = 20
_MAX_EQUITY_POINTS = 20_000
_MAX_DSR_CANDIDATES = 10_000
_MAX_PBO_CANDIDATES = 64


class ResearchRobustnessError(RuntimeError):
    """Structured fail-closed error at the persisted-evidence boundary."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        field: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.field = field

    def detail(self) -> dict[str, Any]:
        detail: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
        }
        if self.field is not None:
            detail["field"] = self.field
        return detail


@dataclass(frozen=True)
class _EquityEvidence:
    dates: tuple[str, ...]
    return_dates: tuple[str, ...]
    returns: np.ndarray
    point_count: int
    initial_equity: float


def _unavailable(
    method: str,
    reason_code: str,
    reason: str,
    *,
    sample_count: Any = None,
    seed: int | None = None,
    assumptions: Mapping[str, Any] | None = None,
    kernel_diagnostic: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "unavailable",
        "method": method,
        "sample_count": sample_count,
        "seed": seed,
        "reason_code": reason_code,
        "reason": reason,
        "assumptions": dict(assumptions or {}),
        "limitations": [
            "Evidence is incomplete; this diagnostic must not be used for "
            "selection or promotion."
        ],
    }
    if kernel_diagnostic is not None:
        result["kernel_diagnostic"] = dict(kernel_diagnostic)
    return result


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


async def _table_names(connection: aiosqlite.Connection) -> set[str]:
    cursor = await connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )
    return {str(row["name"]) for row in await cursor.fetchall()}


async def _table_columns(
    connection: aiosqlite.Connection,
    table: str,
) -> set[str]:
    cursor = await connection.execute(f"PRAGMA table_info({table})")
    return {str(row["name"]) for row in await cursor.fetchall()}


async def _load_manifest(
    connection: aiosqlite.Connection,
    experiment: Mapping[str, Any],
    *,
    required: bool,
) -> tuple[dict[str, Any], str] | None:
    cursor = await connection.execute(
        """
        SELECT user_id, schema_version, manifest_json, manifest_hash
        FROM research_run_manifests WHERE experiment_id=?
        """,
        (experiment["id"],),
    )
    row = await cursor.fetchone()
    if row is None:
        if not required:
            return None
        raise ResearchRobustnessError(
            409,
            "run_manifest_missing",
            "A verified run manifest is required to identify the initial "
            "capital point.",
            field="research_run_manifests",
        )
    try:
        manifest = json.loads(row["manifest_json"])
    except (json.JSONDecodeError, TypeError) as exc:
        raise ResearchRobustnessError(
            409,
            "run_manifest_invalid",
            "Stored run manifest JSON is invalid.",
            field="research_run_manifests.manifest_json",
        ) from exc
    if not isinstance(manifest, dict):
        raise ResearchRobustnessError(
            409,
            "run_manifest_invalid",
            "Stored run manifest must be a JSON object.",
            field="research_run_manifests.manifest_json",
        )
    try:
        calculated_hash = canonical_sha256(manifest)
    except (TypeError, ValueError) as exc:
        raise ResearchRobustnessError(
            409,
            "run_manifest_invalid",
            "Stored run manifest is not finite canonical JSON.",
            field="research_run_manifests.manifest_json",
        ) from exc
    if calculated_hash != row["manifest_hash"]:
        raise ResearchRobustnessError(
            409,
            "run_manifest_integrity_failure",
            "Stored run manifest hash does not match its canonical content.",
            field="research_run_manifests.manifest_hash",
        )
    if (
        row["schema_version"] != RUN_MANIFEST_SCHEMA
        or manifest.get("schema_version") != RUN_MANIFEST_SCHEMA
    ):
        raise ResearchRobustnessError(
            409,
            "run_manifest_schema_unsupported",
            "Run manifest schema is missing or unsupported.",
            field="research_run_manifests.schema_version",
        )
    manifest_experiment = manifest.get("experiment")
    if not isinstance(manifest_experiment, dict):
        manifest_experiment = {}
    if (
        manifest_experiment.get("experiment_id") != experiment["id"]
        or manifest_experiment.get("strategy_id") != experiment["strategy_id"]
        or int(row["user_id"]) != int(experiment["user_id"])
    ):
        raise ResearchRobustnessError(
            409,
            "run_manifest_identity_mismatch",
            "Run manifest identity does not match the persisted experiment.",
            field="research_run_manifests",
        )
    windows = manifest.get("windows")
    if not isinstance(windows, dict) or (
        windows.get("test_start") != experiment["test_start"]
        or windows.get("test_end") != experiment["test_end"]
    ):
        raise ResearchRobustnessError(
            409,
            "run_manifest_window_mismatch",
            "Run manifest test window does not match the experiment.",
            field="research_run_manifests.manifest_json.windows",
        )
    execution = manifest.get("execution")
    initial_capital = (
        execution.get("initial_capital")
        if isinstance(execution, dict)
        else None
    )
    if _finite_number(initial_capital) is None or float(initial_capital) <= 0:
        raise ResearchRobustnessError(
            409,
            "initial_capital_evidence_missing",
            "Run manifest lacks a valid positive initial_capital.",
            field="research_run_manifests.manifest_json.execution.initial_capital",
        )
    return manifest, str(row["manifest_hash"])


async def _load_equity(
    connection: aiosqlite.Connection,
    experiment: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> _EquityEvidence:
    cursor = await connection.execute(
        "SELECT COUNT(*) AS count FROM equity_curve WHERE experiment_id=?",
        (experiment["id"],),
    )
    count = int((await cursor.fetchone())["count"])
    if count > _MAX_EQUITY_POINTS:
        raise ResearchRobustnessError(
            422,
            "equity_curve_too_large",
            f"Equity curve exceeds the {_MAX_EQUITY_POINTS}-point safety cap.",
            field="equity_curve",
        )
    cursor = await connection.execute(
        """
        SELECT id, date, equity, daily_return
        FROM equity_curve WHERE experiment_id=? ORDER BY id
        """,
        (experiment["id"],),
    )
    rows = await cursor.fetchall()
    if len(rows) < _MIN_RETURN_SAMPLES + 1:
        raise ResearchRobustnessError(
            422,
            "insufficient_equity_samples",
            f"At least {_MIN_RETURN_SAMPLES + 1} equity points, including "
            "the initial-capital point, are required.",
            field="equity_curve",
        )

    parsed_dates: list[date] = []
    equities: list[float] = []
    stored_returns: list[float | None] = []
    for row in rows:
        raw_date = row["date"]
        try:
            parsed = date.fromisoformat(str(raw_date))
        except (TypeError, ValueError) as exc:
            raise ResearchRobustnessError(
                422,
                "equity_date_invalid",
                "Equity dates must be strict ISO calendar dates.",
                field="equity_curve.date",
            ) from exc
        if str(raw_date) != parsed.isoformat():
            raise ResearchRobustnessError(
                422,
                "equity_date_invalid",
                "Equity dates must use canonical YYYY-MM-DD form.",
                field="equity_curve.date",
            )
        parsed_dates.append(parsed)
        equity = _finite_number(row["equity"])
        if equity is None or equity <= 0:
            raise ResearchRobustnessError(
                422,
                "equity_value_invalid",
                "All equity values must be finite and strictly positive.",
                field="equity_curve.equity",
            )
        equities.append(equity)
        stored_return = row["daily_return"]
        stored_returns.append(
            None if stored_return is None else _finite_number(stored_return)
        )
        if stored_return is not None and stored_returns[-1] is None:
            raise ResearchRobustnessError(
                422,
                "stored_return_invalid",
                "Stored daily returns must be finite when present.",
                field="equity_curve.daily_return",
            )

    if len(set(parsed_dates)) != len(parsed_dates):
        raise ResearchRobustnessError(
            422,
            "equity_dates_duplicate",
            "Equity curve contains duplicate dates.",
            field="equity_curve.date",
        )
    if any(left >= right for left, right in zip(parsed_dates, parsed_dates[1:])):
        raise ResearchRobustnessError(
            422,
            "equity_dates_not_monotonic",
            "Equity dates must be strictly increasing in persisted order.",
            field="equity_curve.date",
        )

    execution = manifest["execution"]
    initial_capital = float(execution["initial_capital"])
    if stored_returns[0] is not None or not math.isclose(
        equities[0],
        initial_capital,
        rel_tol=1e-12,
        abs_tol=max(1e-8, initial_capital * 1e-12),
    ):
        raise ResearchRobustnessError(
            422,
            "initial_equity_point_missing",
            "The first curve point is not the manifest-bound initial-capital "
            "baseline.",
            field="equity_curve",
        )
    if (parsed_dates[1] - parsed_dates[0]).days != 1:
        raise ResearchRobustnessError(
            422,
            "initial_equity_point_missing",
            "The initial-capital baseline must immediately precede the first "
            "backtest session by one calendar day.",
            field="equity_curve.date",
        )
    try:
        test_start = date.fromisoformat(str(experiment["test_start"]))
        test_end = date.fromisoformat(str(experiment["test_end"]))
    except (TypeError, ValueError) as exc:
        raise ResearchRobustnessError(
            409,
            "experiment_window_invalid",
            "Experiment test window is invalid.",
            field="experiments.test_start",
        ) from exc
    if any(value < test_start or value > test_end for value in parsed_dates[1:]):
        raise ResearchRobustnessError(
            422,
            "equity_outside_test_window",
            "One or more return-bearing equity points fall outside the "
            "manifest-bound test window.",
            field="equity_curve.date",
        )

    # Explicitly sort by date after validating persisted monotonicity. No
    # interpolation, forward fill, or stored-return fallback is permitted.
    ordered = sorted(zip(parsed_dates, equities), key=lambda item: item[0])
    ordered_equity = np.asarray([item[1] for item in ordered], dtype=float)
    returns = ordered_equity[1:] / ordered_equity[:-1] - 1.0
    if (
        not np.isfinite(returns).all()
        or np.any(returns < -1.0)
        or len(returns) < _MIN_RETURN_SAMPLES
    ):
        raise ResearchRobustnessError(
            422,
            "calculated_returns_invalid",
            "Returns calculated from equity failed finite-sample validation.",
            field="equity_curve.equity",
        )
    for position, stored in enumerate(stored_returns[1:], start=1):
        if stored is None or not math.isclose(
            stored,
            float(returns[position - 1]),
            rel_tol=1e-9,
            abs_tol=1e-12,
        ):
            raise ResearchRobustnessError(
                422,
                "stored_return_mismatch",
                "Stored daily_return does not match the return calculated "
                "from adjacent equity points.",
                field=f"equity_curve.daily_return[{position}]",
            )
    return _EquityEvidence(
        dates=tuple(item[0].isoformat() for item in ordered),
        return_dates=tuple(item[0].isoformat() for item in ordered[1:]),
        returns=returns,
        point_count=len(ordered),
        initial_equity=initial_capital,
    )


async def _candidate_context(
    connection: aiosqlite.Connection,
    experiment: Mapping[str, Any],
    tables: set[str],
) -> dict[str, Any]:
    """Resolve exactly one database-owned trial universe, preferring sweeps."""
    if {"param_sweeps", "sweep_experiments"} <= tables:
        cursor = await connection.execute(
            """
            SELECT ps.*
            FROM sweep_experiments se
            JOIN param_sweeps ps ON ps.id=se.sweep_id
            WHERE se.experiment_id=?
            ORDER BY ps.id
            """,
            (experiment["id"],),
        )
        sweeps = await cursor.fetchall()
        relation_kind = "direct_member"
        if not sweeps:
            sweep_columns = await _table_columns(
                connection, "param_sweeps"
            )
            experiment_columns = await _table_columns(
                connection, "experiments"
            )
            if {
                "promoted_experiment_id",
                "promotion_source_experiment_id",
            } <= sweep_columns and "source_experiment_id" in experiment_columns:
                cursor = await connection.execute(
                    """
                    SELECT ps.*
                    FROM param_sweeps ps
                    WHERE ps.promoted_experiment_id=?
                      AND ps.promotion_source_experiment_id=?
                    ORDER BY ps.id
                    """,
                    (
                        experiment["id"],
                        experiment["source_experiment_id"],
                    ),
                )
                sweeps = await cursor.fetchall()
                relation_kind = "promoted_locked_test"
        if len(sweeps) > 1:
            return {
                "source": "ambiguous",
                "relation_type": "parameter_sweep",
                "relation_ids": [int(row["id"]) for row in sweeps],
                "candidate_count": None,
                "candidate_experiment_ids": [],
                "trustworthy": False,
                "reason_code": "multiple_parent_sweeps",
            }
        if len(sweeps) == 1:
            sweep = sweeps[0]
            if (
                int(sweep["user_id"]) != int(experiment["user_id"])
                or sweep["strategy_id"] != experiment["strategy_id"]
            ):
                return {
                    "source": "parameter_sweep",
                    "relation_id": int(sweep["id"]),
                    "candidate_count": None,
                    "candidate_experiment_ids": [],
                    "trustworthy": False,
                    "reason_code": "sweep_identity_mismatch",
                }
            cursor = await connection.execute(
                """
                SELECT e.id, e.user_id, e.strategy_id, e.status,
                       e.test_start, e.test_end, e.params,
                       se.param_combo, m.sharpe_ratio
                FROM sweep_experiments se
                JOIN experiments e ON e.id=se.experiment_id
                LEFT JOIN experiment_metrics m ON m.experiment_id=e.id
                WHERE se.sweep_id=? AND e.status='completed'
                ORDER BY e.id
                """,
                (sweep["id"],),
            )
            candidates = [dict(row) for row in await cursor.fetchall()]
            trustworthy = all(
                int(row["user_id"]) == int(experiment["user_id"])
                and row["strategy_id"] == experiment["strategy_id"]
                for row in candidates
            )
            completed_ids = [int(row["id"]) for row in candidates]
            declared_total = int(sweep["total_experiments"] or 0)
            return {
                "source": "parameter_sweep",
                "relation_id": int(sweep["id"]),
                "candidate_count": len(candidates),
                "candidate_experiment_ids": completed_ids,
                "declared_total_experiments": declared_total,
                "excluded_non_completed_count": max(
                    0, declared_total - len(candidates)
                ),
                "research_trust": sweep["research_trust"],
                "target_relation": relation_kind,
                "selection_window": {
                    "start": sweep["selection_start"],
                    "end": sweep["selection_end"],
                },
                "trustworthy": bool(
                    trustworthy
                    and (
                        int(experiment["id"]) in completed_ids
                        if relation_kind == "direct_member"
                        else int(sweep["promotion_source_experiment_id"])
                        in completed_ids
                    )
                ),
                "reason_code": (
                    None
                    if trustworthy
                    and (
                        int(experiment["id"]) in completed_ids
                        if relation_kind == "direct_member"
                        else int(sweep["promotion_source_experiment_id"])
                        in completed_ids
                    )
                    else "sweep_candidate_identity_mismatch"
                ),
                "_candidates": candidates,
            }

    if {"research_experiment_groups", "research_trials"} <= tables:
        cursor = await connection.execute(
            """
            SELECT DISTINCT g.*
            FROM research_trials t
            JOIN research_experiment_groups g ON g.id=t.group_id
            WHERE t.experiment_id=?
            ORDER BY g.id
            """,
            (experiment["id"],),
        )
        groups = await cursor.fetchall()
        if len(groups) > 1:
            return {
                "source": "ambiguous",
                "relation_type": "research_group",
                "relation_ids": [int(row["id"]) for row in groups],
                "candidate_count": None,
                "candidate_experiment_ids": [],
                "trustworthy": False,
                "reason_code": "multiple_research_groups",
            }
        if len(groups) == 1:
            group = groups[0]
            if (
                int(group["user_id"]) != int(experiment["user_id"])
                or group["strategy_id"] != experiment["strategy_id"]
            ):
                return {
                    "source": "research_group",
                    "relation_id": int(group["id"]),
                    "candidate_count": None,
                    "candidate_experiment_ids": [],
                    "trustworthy": False,
                    "reason_code": "research_group_identity_mismatch",
                }
            cursor = await connection.execute(
                """
                SELECT DISTINCT e.id, e.user_id, e.strategy_id, e.status,
                       e.test_start, e.test_end, e.params,
                       m.sharpe_ratio
                FROM research_trials t
                JOIN experiments e ON e.id=t.experiment_id
                LEFT JOIN experiment_metrics m ON m.experiment_id=e.id
                WHERE t.group_id=? AND e.status='completed'
                ORDER BY e.id
                """,
                (group["id"],),
            )
            candidates = [dict(row) for row in await cursor.fetchall()]
            completed_ids = [int(row["id"]) for row in candidates]
            trustworthy = all(
                int(row["user_id"]) == int(experiment["user_id"])
                and row["strategy_id"] == experiment["strategy_id"]
                for row in candidates
            )
            return {
                "source": "research_group",
                "relation_id": int(group["id"]),
                "candidate_count": len(candidates),
                "candidate_experiment_ids": completed_ids,
                "group_status": group["status"],
                "trustworthy": bool(
                    trustworthy and int(experiment["id"]) in completed_ids
                ),
                "reason_code": (
                    None
                    if trustworthy and int(experiment["id"]) in completed_ids
                    else "research_group_candidate_identity_mismatch"
                ),
                "_candidates": candidates,
            }

    sharpe_ratio: float | None = None
    if "experiment_metrics" in tables:
        cursor = await connection.execute(
            "SELECT sharpe_ratio FROM experiment_metrics WHERE experiment_id=?",
            (experiment["id"],),
        )
        metric = await cursor.fetchone()
        if metric is not None:
            sharpe_ratio = metric["sharpe_ratio"]
    return {
        "source": "independent_experiment",
        "relation_id": None,
        "candidate_count": 1,
        "candidate_experiment_ids": [int(experiment["id"])],
        "trustworthy": True,
        "reason_code": None,
        "_candidates": [{
            "id": int(experiment["id"]),
            "sharpe_ratio": sharpe_ratio,
        }],
    }


def _deflated_sharpe(
    equity: _EquityEvidence,
    context: Mapping[str, Any],
) -> dict[str, Any]:
    count = context.get("candidate_count")
    candidates = context.get("_candidates", [])
    assumptions = {
        "candidate_count_source": (
            "experiment.db database relationship"
            if context.get("source") != "independent_experiment"
            else "no parent sweep or research group relationship"
        ),
        "candidate_relation": context.get("source"),
        "candidate_relation_id": context.get("relation_id"),
        "candidate_count": count,
        "client_supplied_candidate_count": False,
    }
    if not context.get("trustworthy") or not isinstance(count, int):
        return _unavailable(
            "deflated_sharpe_ratio",
            str(context.get("reason_code") or "candidate_relation_untrusted"),
            "A unique, owner- and strategy-consistent candidate relation "
            "could not be established.",
            sample_count=equity.returns.size,
            assumptions=assumptions,
        )
    if count < 1 or count > _MAX_DSR_CANDIDATES:
        return _unavailable(
            "deflated_sharpe_ratio",
            "candidate_count_out_of_bounds",
            "The derived completed-candidate count is outside the safety cap.",
            sample_count=equity.returns.size,
            assumptions=assumptions,
        )
    sharpes = [
        _finite_number(candidate.get("sharpe_ratio"))
        for candidate in candidates
    ]
    if len(sharpes) != count or any(value is None for value in sharpes):
        return _unavailable(
            "deflated_sharpe_ratio",
            "candidate_metric_evidence_incomplete",
            "Every completed related candidate must have a finite persisted "
            "experiment_metrics.sharpe_ratio.",
            sample_count=equity.returns.size,
            assumptions=assumptions,
        )
    kernel = deflated_sharpe_ratio(
        equity.returns,
        [float(value) for value in sharpes if value is not None],
        min_samples=_MIN_RETURN_SAMPLES,
    )
    if kernel.get("status") != "ok":
        reason_code = (
            "independent_candidate_count_one"
            if count == 1
            else "dsr_kernel_rejected_evidence"
        )
        return _unavailable(
            "deflated_sharpe_ratio",
            reason_code,
            (
                "An independent experiment has candidate_count=1; DSR "
                "requires at least two distinct completed trial Sharpes."
                if count == 1
                else "The DSR kernel rejected the derived trial evidence."
            ),
            sample_count=equity.returns.size,
            assumptions=assumptions,
            kernel_diagnostic=kernel,
        )
    kernel["assumptions"].update(assumptions)
    return kernel


async def _pbo(
    connection: aiosqlite.Connection,
    experiment: Mapping[str, Any],
    context: Mapping[str, Any],
    *,
    n_slices: int,
    max_combinations: int,
    seed: int,
) -> dict[str, Any]:
    count = context.get("candidate_count")
    assumptions = {
        "candidate_relation": context.get("source"),
        "candidate_relation_id": context.get("relation_id"),
        "alignment": "exact return-date equality; no interpolation",
        "n_slices": n_slices,
        "max_combinations": max_combinations,
    }
    if context.get("source") != "parameter_sweep":
        return _unavailable(
            "cscv_pbo",
            "same_sweep_required",
            "CSCV/PBO is only constructed from completed trials in one "
            "verified parameter sweep.",
            sample_count=None,
            seed=seed,
            assumptions=assumptions,
        )
    if (
        not context.get("trustworthy")
        or not isinstance(count, int)
        or count < 2
    ):
        return _unavailable(
            "cscv_pbo",
            "insufficient_verified_sweep_candidates",
            "At least two owner- and strategy-consistent completed sweep "
            "candidates are required.",
            sample_count=None,
            seed=seed,
            assumptions=assumptions,
        )
    if count > _MAX_PBO_CANDIDATES:
        return _unavailable(
            "cscv_pbo",
            "pbo_candidate_cap_exceeded",
            f"CSCV/PBO is capped at {_MAX_PBO_CANDIDATES} candidates; no "
            "subset is selected because that would change the trial universe.",
            sample_count={"trials": count, "periods": None},
            seed=seed,
            assumptions=assumptions,
        )

    evidence: list[tuple[int, _EquityEvidence]] = []
    for candidate in context.get("_candidates", []):
        cursor = await connection.execute(
            "SELECT * FROM experiments WHERE id=?",
            (candidate["id"],),
        )
        row = await cursor.fetchone()
        if row is None:
            return _unavailable(
                "cscv_pbo",
                "sweep_candidate_missing",
                "A related sweep candidate no longer exists.",
                seed=seed,
                assumptions=assumptions,
            )
        try:
            manifest_envelope = await _load_manifest(
                connection, row, required=True
            )
            assert manifest_envelope is not None
            candidate_equity = await _load_equity(
                connection, row, manifest_envelope[0]
            )
        except ResearchRobustnessError as exc:
            return _unavailable(
                "cscv_pbo",
                "sweep_candidate_equity_invalid",
                f"Candidate {candidate['id']} failed evidence validation: "
                f"{exc.code}.",
                seed=seed,
                assumptions=assumptions,
            )
        evidence.append((int(candidate["id"]), candidate_equity))

    first_dates = evidence[0][1].return_dates
    if any(item.return_dates != first_dates for _, item in evidence[1:]):
        return _unavailable(
            "cscv_pbo",
            "sweep_return_dates_not_aligned",
            "Sweep candidate return dates are not exactly equal; no "
            "interpolation or imputation was applied.",
            sample_count={"trials": count, "periods": None},
            seed=seed,
            assumptions=assumptions,
        )
    matrix = pd.DataFrame(
        np.vstack([item.returns for _, item in evidence]),
        index=[str(candidate_id) for candidate_id, _ in evidence],
        columns=first_dates,
    )
    kernel = cscv_probability_of_backtest_overfitting(
        matrix,
        n_slices=n_slices,
        max_combinations=max_combinations,
        seed=seed,
    )
    if kernel.get("status") != "ok":
        return _unavailable(
            "cscv_pbo",
            "pbo_kernel_rejected_evidence",
            "The aligned sweep matrix did not satisfy CSCV/PBO sample or "
            "variation requirements.",
            sample_count={
                "trials": int(matrix.shape[0]),
                "periods": int(matrix.shape[1]),
            },
            seed=seed,
            assumptions=assumptions,
            kernel_diagnostic=kernel,
        )
    kernel["assumptions"].update(assumptions)
    return kernel


def _public_context(context: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in context.items()
        if not key.startswith("_")
    }


async def build_robustness_report(
    connection: aiosqlite.Connection,
    experiment: aiosqlite.Row,
    *,
    seed: int,
    n_bootstrap: int,
    bootstrap_method: str,
    n_slices: int,
    max_combinations: int,
) -> dict[str, Any]:
    """Build a deterministic report from one already-open read-only DB."""
    if experiment["status"] != "completed":
        raise ResearchRobustnessError(
            409,
            "experiment_not_completed",
            "Robustness diagnostics require a completed experiment.",
            field="experiments.status",
        )
    tables = await _table_names(connection)
    required_tables = {
        "equity_curve",
        "experiment_metrics",
        "research_run_manifests",
    }
    missing = sorted(required_tables - tables)
    if missing:
        raise ResearchRobustnessError(
            409,
            "research_evidence_tables_missing",
            f"Required research evidence tables are missing: {missing}.",
            field="experiment.db",
        )
    manifest_envelope = await _load_manifest(
        connection, experiment, required=True
    )
    assert manifest_envelope is not None
    manifest, manifest_hash = manifest_envelope
    equity = await _load_equity(connection, experiment, manifest)
    context = await _candidate_context(connection, experiment, tables)

    bootstrap = block_bootstrap_performance(
        equity.returns,
        n_bootstrap=n_bootstrap,
        method=bootstrap_method,
        seed=seed,
        min_samples=_MIN_RETURN_SAMPLES,
    )
    psr = probabilistic_sharpe_ratio(
        equity.returns,
        benchmark_sharpe=0.0,
        min_samples=_MIN_RETURN_SAMPLES,
    )
    dsr = _deflated_sharpe(equity, context)
    pbo = await _pbo(
        connection,
        experiment,
        context,
        n_slices=n_slices,
        max_combinations=max_combinations,
        seed=seed,
    )
    diagnostics = {
        "block_bootstrap": bootstrap,
        "probabilistic_sharpe_ratio": psr,
        "deflated_sharpe_ratio": dsr,
        "cscv_pbo": pbo,
        "parameter_stability": _unavailable(
            "parameter_stability_region",
            "immutable_ranking_metric_missing",
            "Current sweep records do not persist an immutable, "
            "preregistered ranking metric; parameter stability is therefore "
            "not inferred from mutable UI sorting.",
            assumptions={
                "same_source_sweep": context.get("source")
                == "parameter_sweep",
                "locked_ranking_metric_present": False,
            },
        ),
        "cost_stress": _unavailable(
            "cost_stress_test",
            "gross_return_and_turnover_evidence_missing",
            "Net equity does not identify gross returns and period turnover; "
            "cost stress is not reverse-engineered from net performance.",
        ),
        "capacity": _unavailable(
            "capacity_analysis",
            "pit_adv_evidence_missing",
            "Capacity requires immutable point-in-time ADV and period turnover "
            "snapshots, which are not stored with this experiment.",
        ),
        "multiple_testing": _unavailable(
            "multiple_testing_correction",
            "trusted_p_values_missing",
            "No preregistered, immutable p-values are stored for the related "
            "trial universe.",
        ),
    }
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "experiment_id": int(experiment["id"]),
        "analysis_role": "post_hoc_diagnostic",
        "selection_eligible": False,
        "promotion_eligible": False,
        "evidence": {
            "database": "experiment.db",
            "manifest_schema_version": manifest["schema_version"],
            "manifest_hash": manifest_hash,
            "equity_point_count": equity.point_count,
            "return_sample_count": int(equity.returns.size),
            "initial_equity": equity.initial_equity,
            "first_equity_date": equity.dates[0],
            "last_equity_date": equity.dates[-1],
            "returns_source": "adjacent persisted equity points",
            "external_market_data_queried": False,
            "report_persisted": False,
        },
        "request_parameters": {
            "seed": seed,
            "n_bootstrap": n_bootstrap,
            "bootstrap_method": bootstrap_method,
            "n_slices": n_slices,
            "max_combinations": max_combinations,
        },
        "candidate_context": _public_context(context),
        "diagnostics": diagnostics,
        "assumptions": [
            "Daily observations are treated as 252 periods per year.",
            "PSR uses a zero annualized benchmark Sharpe.",
            "Completed related trials define the DSR candidate universe; "
            "failed or unfinished attempts are disclosed but excluded.",
            "All statistics are computed from persisted net equity and do not "
            "establish execution capacity or gross-of-cost performance.",
        ],
        "limitations": [
            "This endpoint is a post-hoc test-window diagnostic and is not "
            "eligible for strategy selection.",
            "Repeated inspection of this report can itself introduce "
            "researcher discretion and is not corrected here.",
            "A diagnostic must be preregistered and frozen into an immutable "
            "research Report before it can be considered by workflow gates.",
        ],
        "workflow_notice": (
            "To use robustness evidence in a future promotion decision, "
            "preregister the metric and protocol before the locked test, then "
            "freeze the result into an immutable workflow Report. This "
            "endpoint never changes the promotion gate."
        ),
    }
