"""Offline research-readiness validation for cached market data and strategies."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
from typing import Any, Iterable, Literal, Mapping

import numpy as np
import pandas as pd

from backend.data.lineage import (
    COUNT_MISMATCH,
    NON_POINT_IN_TIME,
    SURVIVORSHIP_BIAS,
)
from backend.data.generation_manifest import (
    GenerationManifestError,
    GenerationManifestStore,
)
from backend.strategies.base import TrainableStrategy, TrainingWindowContext
from backend.strategies.registry import StrategyRegistry

SCHEMA_VERSION = "research-validation-matrix/v1"
Readiness = Literal[
    "blocked",
    "synthetic_only",
    "cached_real_untrusted",
    "cached_real_validated",
]
SourceKind = Literal["cached_real", "synthetic"]
INDEX_POOLS = {"csi300", "csi500", "csi800", "csi1000"}
VALID_ACTIONS = {"BUY", "SELL", "HOLD"}
VALID_CODE = re.compile(r"^[0-9A-Za-z._-]{1,32}$")
PRICE_FIELDS = ("open", "high", "low", "close")


@dataclass(frozen=True)
class StrategyDataContract:
    required_fields: tuple[str, ...]
    alternative_fields: tuple[tuple[str, ...], ...] = ()
    recommended_fields: tuple[str, ...] = ()
    min_history_rows: int = 120
    min_codes: int = 1
    validation_mode: str = "generate_signals"


DEFAULT_CONTRACT = StrategyDataContract(required_fields=("close",))


def _contracts() -> dict[str, StrategyDataContract]:
    close = DEFAULT_CONTRACT
    training_alpha = StrategyDataContract(
        required_fields=("close",),
        recommended_fields=("open", "high", "low", "volume", "amount"),
        min_history_rows=180,
        min_codes=5,
        validation_mode="ml_prepare_contract",
    )
    sequence = StrategyDataContract(
        required_fields=("close",),
        min_history_rows=300,
        min_codes=2,
        validation_mode="ml_sequence_contract",
    )
    result = {
        strategy_id: close
        for strategy_id in (
            "ma_cross_v1",
            "macd_signal_v1",
            "rsi_reversal_v1",
            "bollinger_breakout_v1",
            "short_reversal_v1",
            "low_volatility_v1",
            "momentum_cross_v1",
            "multi_factor_score_v1",
            "risk_parity_v1",
            "composite_equal_v1",
            "composite_momentum_v1",
            "composite_regime_v1",
            "composite_research_weighted_v1",
            "composite_riskparity_v1",
        )
    }
    result["donchian_breakout_v1"] = StrategyDataContract(
        required_fields=("close",),
        recommended_fields=("high", "low"),
        min_history_rows=80,
    )
    result["liquidity_factor_v1"] = StrategyDataContract(
        required_fields=("close",),
        alternative_fields=(("amount", "volume"),),
        min_history_rows=60,
    )
    result["alphamaster_gbr_v1"] = StrategyDataContract(
        required_fields=("close",),
        recommended_fields=("volume",),
        min_history_rows=300,
        min_codes=5,
        validation_mode="factor_training_contract",
    )
    result.update(
        {
            "alpha158_lgb_v1": training_alpha,
            "alpha158_xgb_v1": training_alpha,
            "alpha158_rank_lgb_v1": training_alpha,
            "lstm_rank_v1": sequence,
            "transformer_rank_v1": sequence,
        }
    )
    return result


STRATEGY_CONTRACTS = _contracts()


def discover_cached_pools(cache_root: Path | str) -> list[str]:
    """List pool parquet names without creating or modifying cache files."""
    daily = Path(cache_root) / "daily"
    if not daily.is_dir():
        return []
    return sorted(
        path.stem
        for path in daily.glob("*.parquet")
        if not path.name.endswith(".tmp")
    )


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _column_axes(
    frame: pd.DataFrame,
) -> tuple[list[str], list[str]]:
    if not isinstance(frame.columns, pd.MultiIndex):
        return (
            [str(column) for column in frame.columns if str(column) != "date"],
            ["close"] if len(frame.columns) else [],
        )
    codes = sorted(
        {
            str(column[0]).strip()
            for column in frame.columns
            if isinstance(column, tuple) and str(column[0]).strip()
        }
    )
    fields = sorted(
        {
            str(column[-1]).strip().lower()
            for column in frame.columns
            if isinstance(column, tuple) and str(column[-1]).strip()
        }
    )
    return codes, fields


def _field_frame(frame: pd.DataFrame, field: str) -> pd.DataFrame:
    if not isinstance(frame.columns, pd.MultiIndex):
        return (
            frame.drop(columns=["date"], errors="ignore")
            if field == "close"
            else pd.DataFrame(index=frame.index)
        )
    values: dict[str, pd.Series] = {}
    for position, column in enumerate(frame.columns):
        if (
            isinstance(column, tuple)
            and str(column[-1]).strip().lower() == field
        ):
            values.setdefault(str(column[0]), frame.iloc[:, position])
    return pd.DataFrame(values, index=frame.index)


def audit_market_frame(
    frame: pd.DataFrame,
    *,
    pool_id: str,
    metadata: Mapping[str, Any] | None = None,
    membership: Mapping[str, Any] | None = None,
    source_kind: SourceKind = "cached_real",
    point_in_time: bool | None = None,
) -> dict[str, Any]:
    """Run conservative data-quality and provenance gates."""
    metadata = dict(metadata or {})
    membership = dict(membership or {})
    codes, fields = _column_axes(frame)
    issues: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []

    def issue(code: str, detail: str) -> None:
        issues.append({"code": code, "detail": detail})

    def warn(code: str, detail: str) -> None:
        warnings.append({"code": code, "detail": detail})

    if frame.empty:
        issue("empty_dataset", "缓存数据为空")
    if not isinstance(frame.index, pd.DatetimeIndex):
        issue("non_datetime_index", "索引不是 DatetimeIndex")
        parsed_index = pd.to_datetime(frame.index, errors="coerce")
    else:
        parsed_index = frame.index
    if getattr(parsed_index, "hasnans", False):
        issue("invalid_dates", "日期索引包含无法解析值")
    if not parsed_index.is_monotonic_increasing:
        issue("non_monotonic_dates", "日期索引不是单调递增")
    duplicate_dates = int(parsed_index.duplicated(keep=False).sum())
    if duplicate_dates:
        issue("duplicate_dates", f"重复日期行数={duplicate_dates}")
    duplicate_columns = int(frame.columns.duplicated(keep=False).sum())
    if duplicate_columns:
        issue("duplicate_columns", f"重复列数={duplicate_columns}")
    if not isinstance(frame.columns, pd.MultiIndex):
        warn("legacy_single_level_columns", "不是标准 (code, field) 多级列")

    numeric = frame.apply(pd.to_numeric, errors="coerce")
    total_cells = int(numeric.size)
    nan_cells = int(numeric.isna().sum().sum())
    nan_ratio = float(nan_cells / total_cells) if total_cells else 1.0
    if nan_cells:
        issue("nan_values", f"NaN 单元格={nan_cells}, 比例={nan_ratio:.6f}")

    negative_price_cells = 0
    for field in PRICE_FIELDS:
        values = _field_frame(numeric, field)
        if not values.empty:
            negative_price_cells += int((values < 0).sum().sum())
    if negative_price_cells:
        issue(
            "negative_prices",
            f"价格字段负值单元格={negative_price_cells}",
        )
    negative_volume_cells = 0
    for field in ("volume", "amount"):
        values = _field_frame(numeric, field)
        if not values.empty:
            negative_volume_cells += int((values < 0).sum().sum())
    if negative_volume_cells:
        issue(
            "negative_volume_or_amount",
            f"成交量/成交额负值单元格={negative_volume_cells}",
        )

    open_frame = _field_frame(numeric, "open")
    high_frame = _field_frame(numeric, "high")
    low_frame = _field_frame(numeric, "low")
    close_frame = _field_frame(numeric, "close")
    common_codes = sorted(
        set(open_frame)
        & set(high_frame)
        & set(low_frame)
        & set(close_frame)
    )
    ohlc_violations = 0
    if common_codes:
        open_values = open_frame[common_codes]
        high_values = high_frame[common_codes]
        low_values = low_frame[common_codes]
        close_values = close_frame[common_codes]
        valid = (
            open_values.notna()
            & high_values.notna()
            & low_values.notna()
            & close_values.notna()
        )
        invalid = valid & (
            (high_values < open_values)
            | (high_values < close_values)
            | (low_values > open_values)
            | (low_values > close_values)
            | (high_values < low_values)
        )
        ohlc_violations = int(invalid.sum().sum())
    if ohlc_violations:
        issue("ohlcv_logic", f"OHLC 逻辑冲突单元格={ohlc_violations}")

    expected_count = membership.get("count")
    expected_count = (
        int(expected_count)
        if isinstance(expected_count, (int, float))
        and not isinstance(expected_count, bool)
        else None
    )
    count_difference = (
        len(codes) - expected_count if expected_count is not None else None
    )
    if count_difference not in (None, 0):
        issue(
            COUNT_MISMATCH,
            f"缓存代码数={len(codes)}, 成分清单数={expected_count}",
        )

    meta_dates = (
        metadata.get("date_start"),
        metadata.get("date_end"),
    )
    actual_dates = (
        (
            pd.Timestamp(parsed_index.min()).date().isoformat()
            if len(parsed_index)
            else None
        ),
        (
            pd.Timestamp(parsed_index.max()).date().isoformat()
            if len(parsed_index)
            else None
        ),
    )
    if all(meta_dates) and tuple(meta_dates) != actual_dates:
        warn(
            "metadata_date_mismatch",
            f"meta={meta_dates[0]}..{meta_dates[1]}, "
            f"actual={actual_dates[0]}..{actual_dates[1]}",
        )

    asserted_pit = (
        bool(point_in_time)
        if point_in_time is not None
        else bool(metadata.get("point_in_time", False))
    )
    # A mutable cache flag or CLI argument is not historical-membership
    # evidence.  Until a verified membership timeline is supported, real
    # caches must remain non-PIT; synthetic fixtures may still describe their
    # own test contract but can never become deployable research.
    pit_claim = asserted_pit if source_kind == "synthetic" else False
    risk_warnings: list[str] = []
    if source_kind == "cached_real" and asserted_pit:
        warn(
            "unverified_point_in_time_claim",
            "可变缓存元数据或调用参数不能证明历史成分股时点有效性",
        )
    if pool_id.lower() in INDEX_POOLS and not pit_claim:
        risk_warnings.extend([NON_POINT_IN_TIME, SURVIVORSHIP_BIAS])
        warn(
            NON_POINT_IN_TIME,
            "指数池缺少历史时点成分快照，存在幸存者偏差",
        )
    if source_kind == "synthetic":
        provenance = "synthetic"
    else:
        provenance = str(metadata.get("source_kind") or "cached_real")
        if provenance != "cached_real":
            issue(
                "unverified_provenance",
                f"缓存来源声明={provenance!r}",
            )

    critical_issue_codes = {
        item["code"]
        for item in issues
    }
    quality_passed = not critical_issue_codes
    return {
        "pool_id": pool_id,
        "source_kind": source_kind,
        "provenance": provenance,
        "point_in_time": pit_claim,
        "rows": int(len(frame)),
        "codes": len(codes),
        "fields": fields,
        "date_start": actual_dates[0],
        "date_end": actual_dates[1],
        "expected_codes": expected_count,
        "count_difference": count_difference,
        "nan_cells": nan_cells,
        "nan_ratio": nan_ratio,
        "duplicate_dates": duplicate_dates,
        "duplicate_columns": duplicate_columns,
        "negative_price_cells": negative_price_cells,
        "negative_volume_or_amount_cells": negative_volume_cells,
        "ohlc_violations": ohlc_violations,
        "issues": issues,
        "warnings": warnings,
        "risk_warnings": sorted(set(risk_warnings)),
        "quality_passed": quality_passed,
    }


def load_cached_dataset(
    cache_root: Path | str,
    pool_id: str,
    *,
    source_kind: SourceKind = "cached_real",
    point_in_time: bool | None = None,
) -> tuple[pd.DataFrame | None, dict[str, Any]]:
    """Load one parquet cache directly; never invoke fetch/update paths."""
    root = Path(cache_root)
    daily_root = root / "daily"
    try:
        view = GenerationManifestStore(
            daily_root,
            required_artifacts={"pivot", "metadata"},
        ).load(pool_id)
    except GenerationManifestError:
        view = None
    parquet = view.artifacts["pivot"] if view is not None else None
    metadata = (
        _read_json(view.artifacts["metadata"])
        if view is not None
        else {}
    )
    membership = _read_json(root / f"pool_{pool_id}.json")
    if parquet is None or not parquet.is_file():
        return None, {
            "pool_id": pool_id,
            "source_kind": source_kind,
            "provenance": source_kind,
            "point_in_time": False,
            "rows": 0,
            "codes": 0,
            "fields": [],
            "date_start": None,
            "date_end": None,
            "expected_codes": membership.get("count"),
            "count_difference": None,
            "nan_cells": 0,
            "nan_ratio": 1.0,
            "duplicate_dates": 0,
            "duplicate_columns": 0,
            "negative_price_cells": 0,
            "negative_volume_or_amount_cells": 0,
            "ohlc_violations": 0,
            "issues": [
                {
                    "code": "cache_missing",
                "detail": f"{pool_id} 没有完整、已激活的缓存代",
                }
            ],
            "warnings": [],
            "risk_warnings": [],
            "quality_passed": False,
        }
    try:
        frame = pd.read_parquet(parquet)
    except Exception as exc:
        return None, {
            "pool_id": pool_id,
            "source_kind": source_kind,
            "provenance": source_kind,
            "point_in_time": False,
            "rows": 0,
            "codes": 0,
            "fields": [],
            "date_start": None,
            "date_end": None,
            "expected_codes": membership.get("count"),
            "count_difference": None,
            "nan_cells": 0,
            "nan_ratio": 1.0,
            "duplicate_dates": 0,
            "duplicate_columns": 0,
            "negative_price_cells": 0,
            "negative_volume_or_amount_cells": 0,
            "ohlc_violations": 0,
            "issues": [
                {
                    "code": "cache_unreadable",
                    "detail": f"Parquet 读取失败: {type(exc).__name__}",
                }
            ],
            "warnings": [],
            "risk_warnings": [],
            "quality_passed": False,
        }
    if not isinstance(frame.index, pd.DatetimeIndex):
        frame.index = pd.to_datetime(frame.index, errors="coerce")
    audit = audit_market_frame(
        frame,
        pool_id=pool_id,
        metadata=metadata,
        membership=membership,
        source_kind=source_kind,
        point_in_time=point_in_time,
    )
    return frame, audit


def _sample_frame(
    frame: pd.DataFrame,
    *,
    max_rows: int,
    max_codes: int,
) -> pd.DataFrame:
    sampled = frame.sort_index().tail(max_rows).copy(deep=True)
    if isinstance(sampled.columns, pd.MultiIndex):
        codes, _ = _column_axes(sampled)
        selected = set(codes[:max_codes])
        sampled = sampled.loc[
            :,
            [
                column
                for column in sampled.columns
                if str(column[0]) in selected
            ],
        ]
    elif len(sampled.columns) > max_codes:
        sampled = sampled.iloc[:, :max_codes]
    return sampled


def _default_params(metadata: Any) -> dict[str, Any]:
    return {
        field.name: field.default
        for field in metadata.params
        if field.default is not None
    }


def _canonical_signals(
    signals: Mapping[str, Iterable[Any]],
    *,
    through: pd.Timestamp | None = None,
) -> list[tuple[str, str, str, float, float]]:
    canonical: list[tuple[str, str, str, float, float]] = []
    for date_text, items in signals.items():
        date = pd.Timestamp(date_text)
        if through is not None and date > through:
            continue
        for item in items:
            canonical.append(
                (
                    date.date().isoformat(),
                    str(item.code),
                    str(item.action).upper(),
                    round(float(item.score), 12),
                    round(float(item.weight), 12),
                )
            )
    return sorted(canonical)


def _validate_signal_output(
    signals: Mapping[str, Iterable[Any]],
    *,
    frame: pd.DataFrame,
    end_date: pd.Timestamp,
) -> tuple[bool, dict[str, Any], list[str]]:
    dates = pd.DatetimeIndex(pd.to_datetime(frame.index)).sort_values().unique()
    codes, _ = _column_axes(frame)
    code_set = set(codes)
    reasons: list[str] = []
    signal_count = 0
    signal_dates: set[pd.Timestamp] = set()
    action_counts: Counter[str] = Counter()
    for date_text, items in signals.items():
        try:
            date = pd.Timestamp(date_text)
        except Exception:
            reasons.append(f"invalid_signal_date:{date_text}")
            continue
        signal_dates.add(date)
        if date > end_date:
            reasons.append(f"signal_after_end:{date.date()}")
        if date not in dates:
            reasons.append(f"signal_date_not_observable:{date.date()}")
        next_positions = dates.searchsorted(date, side="right")
        if next_positions >= len(dates):
            reasons.append(f"missing_t_plus_one_session:{date.date()}")
        for item in items:
            signal_count += 1
            action = str(item.action).upper()
            action_counts[action] += 1
            if action not in VALID_ACTIONS:
                reasons.append(f"invalid_action:{action}")
            code = str(item.code)
            if not VALID_CODE.fullmatch(code):
                reasons.append(f"invalid_code:{code}")
            if code not in code_set:
                reasons.append(f"code_not_in_dataset:{code}")
            for name, value in (
                ("score", item.score),
                ("weight", item.weight),
            ):
                try:
                    finite = math.isfinite(float(value))
                except (TypeError, ValueError):
                    finite = False
                if not finite:
                    reasons.append(f"non_finite_{name}:{code}")
            try:
                weight = float(item.weight)
            except (TypeError, ValueError):
                weight = -1.0
            if not 0 <= weight <= 1:
                reasons.append(f"weight_out_of_range:{code}")
    unique_reasons = sorted(set(reasons))
    return (
        not unique_reasons,
        {
            "signal_dates": len(signal_dates),
            "signals": signal_count,
            "actions": dict(sorted(action_counts.items())),
            "t_plus_one_checked": True,
            "t_plus_one_evidence": (
                f"checked {len(signal_dates)} signal dates against a later "
                "cached session"
            ),
        },
        unique_reasons,
    )


def _mutate_future(
    frame: pd.DataFrame,
    decision_date: pd.Timestamp,
) -> pd.DataFrame:
    mutated = frame.copy(deep=True)
    future_mask = pd.DatetimeIndex(mutated.index) > decision_date
    numeric_columns = mutated.select_dtypes(include=[np.number]).columns
    if len(numeric_columns):
        mutated.loc[future_mask, numeric_columns] = (
            mutated.loc[future_mask, numeric_columns] * 1.137 + 0.031
        )
    return mutated


def _compare_frames_through(
    first: pd.DataFrame,
    second: pd.DataFrame,
    decision_date: pd.Timestamp,
) -> bool:
    left = first.loc[:decision_date].sort_index(axis=1)
    right = second.loc[:decision_date].sort_index(axis=1)
    try:
        pd.testing.assert_frame_equal(
            left,
            right,
            check_exact=False,
            rtol=1e-10,
            atol=1e-12,
        )
    except AssertionError:
        return False
    return True


def _training_no_future_probe(
    registry: StrategyRegistry,
    strategy_id: str,
    sample: pd.DataFrame,
    params: dict[str, Any],
    decision_date: pd.Timestamp,
) -> tuple[bool, str]:
    mutated = _mutate_future(sample, decision_date)
    if strategy_id.startswith("alpha158_"):
        first = registry.create_strategy(strategy_id)
        second = registry.create_strategy(strategy_id)
        first.prepare(sample, params)
        second.prepare(mutated, params)
        first_factors = getattr(first, "_factor_df", None)
        second_factors = getattr(second, "_factor_df", None)
        if not isinstance(first_factors, pd.DataFrame) or not isinstance(
            second_factors,
            pd.DataFrame,
        ):
            return False, "prepare 未产生可比较因子"
        return (
            _compare_frames_through(
                first_factors,
                second_factors,
                decision_date,
            ),
            "Alpha158 prepare 因子截断比较",
        )
    if strategy_id in {"lstm_rank_v1", "transformer_rank_v1"}:
        first = registry.create_strategy(strategy_id)
        second = registry.create_strategy(strategy_id)
        seq_len = int(params.get("seq_len", 20))
        start = str(sample.index[0].date())
        end = str(decision_date.date())
        first_x, first_y = first._build_sequences(  # type: ignore[attr-defined]
            sample,
            seq_len,
            start,
            end,
        )
        second_x, second_y = second._build_sequences(  # type: ignore[attr-defined]
            mutated,
            seq_len,
            start,
            end,
        )
        return (
            np.array_equal(first_x, second_x, equal_nan=True)
            and np.array_equal(first_y, second_y, equal_nan=True),
            "序列与标签窗口截断比较",
        )
    if strategy_id == "alphamaster_gbr_v1":
        from backend.strategies.factor.alphamaster_gbr import (
            _compute_alpha_master_factors,
        )

        first = _compute_alpha_master_factors(sample)
        second = _compute_alpha_master_factors(mutated)
        return (
            _compare_frames_through(first, second, decision_date),
            "AlphaMaster 因子截断比较",
        )
    return False, "没有训练型 no-future 探针"


def _validation_window_probe(
    strategy: TrainableStrategy,
    sample: pd.DataFrame,
    params: dict[str, Any],
    decision_date: pd.Timestamp,
) -> tuple[bool, str]:
    dates = pd.DatetimeIndex(sample.index).sort_values().unique()
    horizon = strategy.label_horizon_days(params)
    if len(dates) < horizon + 40:
        return False, "历史不足以构造 train/validation 窗口"
    validation_end_position = int(
        dates.searchsorted(decision_date, side="right")
    ) - 1
    validation_start_position = max(
        2,
        validation_end_position - horizon - 10,
    )
    train_end_position = validation_start_position - 1
    context = TrainingWindowContext(
        train_start=str(dates[0].date()),
        train_end=str(dates[train_end_position].date()),
        validation_start=str(dates[validation_start_position].date()),
        validation_end=str(dates[validation_end_position].date()),
    )
    _, sample_end = strategy.validation_sample_window(
        sample,
        params,
        context,
    )
    sample_end_position = int(
        dates.get_loc(pd.Timestamp(sample_end))
    )
    valid = (
        sample_end_position + horizon
        <= validation_end_position
        and train_end_position < validation_start_position
    )
    return valid, "验证标签完整落在 validation_end 以内"


def validate_strategy_on_dataset(
    registry: StrategyRegistry,
    strategy_id: str,
    frame: pd.DataFrame | None,
    dataset_audit: Mapping[str, Any],
    *,
    max_rows: int = 420,
    max_codes: int = 12,
) -> dict[str, Any]:
    metadata = registry.get_metadata(strategy_id)
    contract = STRATEGY_CONTRACTS.get(strategy_id, DEFAULT_CONTRACT)
    fields = set(str(item) for item in dataset_audit.get("fields", []))
    missing_fields = sorted(set(contract.required_fields) - fields)
    missing_alternatives = [
        list(group)
        for group in contract.alternative_fields
        if not set(group).intersection(fields)
    ]
    reasons: list[str] = []
    if missing_fields:
        reasons.append(f"missing_fields:{','.join(missing_fields)}")
    if missing_alternatives:
        reasons.extend(
            "missing_any_field:" + "|".join(group)
            for group in missing_alternatives
        )
    if int(dataset_audit.get("rows", 0)) < contract.min_history_rows:
        reasons.append(
            f"insufficient_history:{dataset_audit.get('rows', 0)}"
            f"<{contract.min_history_rows}"
        )
    if int(dataset_audit.get("codes", 0)) < contract.min_codes:
        reasons.append(
            f"insufficient_codes:{dataset_audit.get('codes', 0)}"
            f"<{contract.min_codes}"
        )

    runtime_status = "skipped"
    no_future_status = "skipped"
    no_future_evidence = "not_run"
    signal_checks: dict[str, Any] = {
        "signal_dates": 0,
        "signals": 0,
        "actions": {},
        "t_plus_one_checked": False,
        "t_plus_one_evidence": "not_run",
    }
    if frame is None:
        reasons.append("cache_unavailable")
    elif not reasons:
        sample = _sample_frame(
            frame,
            max_rows=max_rows,
            max_codes=max_codes,
        )
        dates = pd.DatetimeIndex(sample.index).sort_values().unique()
        if len(dates) < 4:
            reasons.append("validation_sample_too_short")
        else:
            params = _default_params(metadata)
            strategy = registry.create_strategy(strategy_id)
            valid_params, param_message = strategy.validate_params(params)
            if not valid_params:
                reasons.append(f"default_params_invalid:{param_message}")
            elif metadata.requires_training:
                signal_checks["t_plus_one_evidence"] = (
                    "not_applicable: training contract probe emits no orders"
                )
                try:
                    decision_date = dates[-30] if len(dates) >= 30 else dates[-2]
                    no_future_ok, no_future_detail = (
                        _training_no_future_probe(
                            registry,
                            strategy_id,
                            sample,
                            params,
                            decision_date,
                        )
                    )
                    no_future_status = (
                        "passed" if no_future_ok else "failed"
                    )
                    no_future_evidence = no_future_detail
                    if not no_future_ok:
                        reasons.append(
                            f"no_future_failed:{no_future_detail}"
                        )
                    if isinstance(strategy, TrainableStrategy):
                        validation_ok, validation_detail = (
                            _validation_window_probe(
                                strategy,
                                sample,
                                params,
                                decision_date,
                            )
                        )
                        if not validation_ok:
                            reasons.append(
                                f"validation_window_failed:{validation_detail}"
                            )
                        no_future_evidence += (
                            f"; validation_window={validation_detail}"
                        )
                    runtime_status = "contract_validated"
                except Exception as exc:
                    runtime_status = "failed"
                    no_future_status = "failed"
                    no_future_evidence = (
                        f"probe_error:{type(exc).__name__}"
                    )
                    reasons.append(
                        f"training_contract_error:{type(exc).__name__}:{exc}"
                    )
            else:
                start_position = max(
                    0,
                    len(dates) - max(contract.min_history_rows, 180),
                )
                start_date = dates[start_position]
                end_date = dates[-2]
                decision_date = (
                    dates[-40] if len(dates) >= 40 else dates[-3]
                )
                try:
                    signals = strategy.generate_batch_signals(
                        sample,
                        params,
                        str(start_date.date()),
                        str(end_date.date()),
                    )
                    output_ok, signal_checks, output_reasons = (
                        _validate_signal_output(
                            signals,
                            frame=sample,
                            end_date=end_date,
                        )
                    )
                    if not output_ok:
                        reasons.extend(output_reasons)
                    runtime_status = "passed" if output_ok else "failed"

                    mutated_strategy = registry.create_strategy(strategy_id)
                    mutated_signals = mutated_strategy.generate_batch_signals(
                        _mutate_future(sample, decision_date),
                        params,
                        str(start_date.date()),
                        str(end_date.date()),
                    )
                    original_before = _canonical_signals(
                        signals,
                        through=decision_date,
                    )
                    mutated_before = _canonical_signals(
                        mutated_signals,
                        through=decision_date,
                    )
                    mutation_ok = original_before == mutated_before

                    full_as_of = registry.create_strategy(
                        strategy_id
                    ).generate_batch_signals(
                        sample,
                        params,
                        str(start_date.date()),
                        str(decision_date.date()),
                    )
                    truncated_as_of = registry.create_strategy(
                        strategy_id
                    ).generate_batch_signals(
                        sample.loc[:decision_date],
                        params,
                        str(start_date.date()),
                        str(decision_date.date()),
                    )
                    full_as_of_canonical = _canonical_signals(
                        full_as_of,
                        through=decision_date,
                    )
                    truncated_canonical = _canonical_signals(
                        truncated_as_of,
                        through=decision_date,
                    )
                    truncation_ok = (
                        full_as_of_canonical == truncated_canonical
                    )
                    no_future_ok = mutation_ok and truncation_ok
                    no_future_status = (
                        "passed" if no_future_ok else "failed"
                    )
                    no_future_evidence = (
                        f"future_mutation={'passed' if mutation_ok else 'failed'}"
                        f"({len(original_before)} vs {len(mutated_before)}); "
                        "as_of_truncation="
                        f"{'passed' if truncation_ok else 'failed'}"
                        f"({len(full_as_of_canonical)} vs "
                        f"{len(truncated_canonical)} canonical signals through "
                        f"{decision_date.date()})"
                    )
                    if not no_future_ok:
                        if not mutation_ok:
                            reasons.append(
                                "no_future_failed:future mutation changed "
                                "earlier signals"
                            )
                        if not truncation_ok:
                            reasons.append(
                                "no_future_failed:full and truncated frames "
                                "differ at the same as-of date"
                            )
                except Exception as exc:
                    runtime_status = "failed"
                    no_future_status = "failed"
                    no_future_evidence = (
                        f"probe_error:{type(exc).__name__}"
                    )
                    reasons.append(
                        f"strategy_runtime_error:{type(exc).__name__}:{exc}"
                    )

    blocking = bool(reasons)
    source_kind = str(dataset_audit.get("source_kind", "cached_real"))
    if blocking:
        readiness: Readiness = "blocked"
    elif source_kind == "synthetic":
        readiness = "synthetic_only"
    elif (
        not dataset_audit.get("quality_passed", False)
        or not dataset_audit.get("point_in_time", False)
        or dataset_audit.get("risk_warnings")
    ):
        readiness = "cached_real_untrusted"
    else:
        readiness = "cached_real_validated"
    if (
        dataset_audit.get("pool_id", "").lower() in INDEX_POOLS
        and not dataset_audit.get("point_in_time", False)
        and readiness == "cached_real_validated"
    ):
        readiness = "cached_real_untrusted"

    readiness_reasons = list(reasons)
    readiness_reasons.extend(
        f"dataset_quality:{item['code']}"
        for item in dataset_audit.get("issues", [])
    )
    readiness_reasons.extend(
        f"dataset_warning:{item['code']}"
        for item in dataset_audit.get("warnings", [])
    )
    readiness_reasons.extend(
        f"dataset_risk:{item}"
        for item in dataset_audit.get("risk_warnings", [])
    )
    if source_kind == "synthetic":
        readiness_reasons.append("source_is_explicitly_synthetic")

    mode = metadata.portfolio_signal_mode
    return {
        "pool_id": dataset_audit.get("pool_id"),
        "strategy_id": strategy_id,
        "category": (
            metadata.category.value
            if hasattr(metadata.category, "value")
            else str(metadata.category)
        ),
        "requires_training": bool(metadata.requires_training),
        "portfolio_signal_mode": (
            mode.value if hasattr(mode, "value") else str(mode)
        ),
        "data_contract": asdict(contract),
        "data_available": frame is not None,
        "missing_fields": missing_fields,
        "missing_alternative_groups": missing_alternatives,
        "runtime_status": runtime_status,
        "no_future_status": no_future_status,
        "no_future_evidence": no_future_evidence,
        "signal_checks": signal_checks,
        "readiness": readiness,
        "deployable_research": readiness == "cached_real_validated",
        "reasons": sorted(set(reasons)),
        "readiness_reasons": sorted(set(readiness_reasons)),
    }


def build_validation_matrix(
    cache_root: Path | str,
    *,
    pool_ids: Iterable[str] | None = None,
    source_kind: SourceKind = "cached_real",
    point_in_time: bool | None = None,
    max_rows: int = 420,
    max_codes: int = 12,
) -> dict[str, Any]:
    """Build a complete registry × cached-pool matrix offline."""
    if source_kind not in {"cached_real", "synthetic"}:
        raise ValueError("source_kind 必须是 cached_real 或 synthetic")
    if max_rows < 120 or max_codes < 2:
        raise ValueError("max_rows 必须 >=120 且 max_codes 必须 >=2")
    registry = StrategyRegistry()
    registry.scan_directory(Path(__file__).resolve().parents[1] / "strategies")
    metadata = sorted(
        registry.list_all(),
        key=lambda item: item.strategy_id,
    )
    strategy_ids = [item.strategy_id for item in metadata]
    missing_contracts = sorted(set(strategy_ids) - set(STRATEGY_CONTRACTS))
    extra_contracts = sorted(set(STRATEGY_CONTRACTS) - set(strategy_ids))
    pools = sorted(
        set(pool_ids)
        if pool_ids is not None
        else set(discover_cached_pools(cache_root))
    )
    if not pools:
        pools = ["csi300", "csi500", "csi800", "csi1000"]

    datasets: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    for pool_id in pools:
        frame, audit = load_cached_dataset(
            cache_root,
            pool_id,
            source_kind=source_kind,
            point_in_time=point_in_time,
        )
        datasets.append(audit)
        for strategy_id in strategy_ids:
            rows.append(
                validate_strategy_on_dataset(
                    registry,
                    strategy_id,
                    frame,
                    audit,
                    max_rows=max_rows,
                    max_codes=max_codes,
                )
            )
        del frame

    readiness_counts = Counter(row["readiness"] for row in rows)
    runtime_counts = Counter(row["runtime_status"] for row in rows)
    no_future_counts = Counter(row["no_future_status"] for row in rows)
    report = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "offline_read_only": True,
        "source_kind": source_kind,
        "strategy_count": len(strategy_ids),
        "pool_count": len(pools),
        "matrix_row_count": len(rows),
        "contract_coverage": {
            "covered": len(strategy_ids) - len(missing_contracts),
            "missing_strategy_ids": missing_contracts,
            "orphan_contract_ids": extra_contracts,
        },
        "summary": {
            "readiness": dict(sorted(readiness_counts.items())),
            "runtime_status": dict(sorted(runtime_counts.items())),
            "no_future_status": dict(sorted(no_future_counts.items())),
        },
        "datasets": datasets,
        "rows": rows,
    }
    json.dumps(report, allow_nan=False, ensure_ascii=False)
    return report


def render_markdown(report: Mapping[str, Any]) -> str:
    """Render a portable report without filesystem paths or secrets."""
    summary = report["summary"]
    lines = [
        "# Research Validation Matrix",
        "",
        f"- Schema: `{report['schema_version']}`",
        f"- Generated at: `{report['generated_at']}`",
        f"- Offline/read-only: `{str(report['offline_read_only']).lower()}`",
        f"- Strategies: {report['strategy_count']}",
        f"- Pools: {report['pool_count']}",
        f"- Matrix rows: {report['matrix_row_count']}",
        (
            "- Readiness: "
            + ", ".join(
                f"`{key}`={value}"
                for key, value in summary["readiness"].items()
            )
        ),
        "",
        "## Dataset Quality",
        "",
        "| Pool | Source | PIT | Rows | Codes | Fields | Quality | Risks |",
        "|---|---|---:|---:|---:|---|---|---|",
    ]
    for dataset in report["datasets"]:
        issue_codes = ",".join(
            item["code"] for item in dataset["issues"]
        ) or "none"
        risks = ",".join(dataset["risk_warnings"]) or "none"
        lines.append(
            "| {pool} | {source} | {pit} | {rows} | {codes} | {fields} | "
            "{quality} | {risks} |".format(
                pool=dataset["pool_id"],
                source=dataset["source_kind"],
                pit=str(dataset["point_in_time"]).lower(),
                rows=dataset["rows"],
                codes=dataset["codes"],
                fields=", ".join(dataset["fields"]),
                quality=issue_codes,
                risks=risks,
            )
        )
    lines.extend(
        [
            "",
            "## Strategy Matrix",
            "",
            "| Pool | Strategy | Category | Contract | Runtime | T+1 evidence | "
            "No-future evidence | Deployable | Readiness | Reasons |",
            "|---|---|---|---|---|---|---|---:|---|---|",
        ]
    )
    for row in report["rows"]:
        contract = row["data_contract"]
        fields = ",".join(contract["required_fields"])
        if contract["alternative_fields"]:
            fields += ";" + ";".join(
                "|".join(group)
                for group in contract["alternative_fields"]
            )
        reasons = (
            "<br>".join(row["readiness_reasons"]).replace("|", "\\|")
            or "none"
        )
        t_plus_one = str(
            row["signal_checks"]["t_plus_one_evidence"]
        ).replace("|", "\\|")
        no_future = str(row["no_future_evidence"]).replace("|", "\\|")
        lines.append(
            "| {pool} | `{strategy}` | {category} | {fields} / {history}d | "
            "{runtime} | {t_plus_one} | {future}: {future_evidence} | "
            "{deployable} | **{readiness}** | {reasons} |".format(
                pool=row["pool_id"],
                strategy=row["strategy_id"],
                category=row["category"],
                fields=fields.replace("|", "\\|"),
                history=contract["min_history_rows"],
                runtime=row["runtime_status"],
                t_plus_one=t_plus_one,
                future=row["no_future_status"],
                future_evidence=no_future,
                deployable=str(row["deployable_research"]).lower(),
                readiness=row["readiness"],
                reasons=reasons,
            )
        )
    lines.extend(
        [
            "",
            "## Readiness Rules",
            "",
            "- `blocked`: required data/runtime/no-future contract failed.",
            "- `synthetic_only`: checks passed only on explicitly synthetic data.",
            "- `cached_real_untrusted`: cached real data passed runtime checks but "
            "quality, provenance, or PIT lineage is incomplete.",
            "- `cached_real_validated`: cached real data passed all gates and has "
            "a point-in-time universe claim.",
            "- Non-PIT preset index pools can never be marked "
            "`cached_real_validated`.",
            "",
        ]
    )
    return "\n".join(lines)
