"""Deterministic, JSON-safe factor research computations.

Public functions accept date × code pandas panels (or a panel payload returned
by this module), never mutate caller-owned inputs, and return only JSON-safe
Python containers. Cross-sectional operations are fitted independently per
date so no observation can influence another date's preprocessing.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any, Literal

import numpy as np
import pandas as pd

PanelPayload = dict[str, Any]
PanelInput = pd.DataFrame | PanelPayload

_EPSILON = 1e-12


def _safe_number(value: Any) -> float | int | None:
    if value is None or value is pd.NA:
        return None
    if isinstance(value, (np.integer, int)) and not isinstance(value, bool):
        return int(value)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _date_text(value: Any) -> str:
    timestamp = pd.Timestamp(value)
    if timestamp == timestamp.normalize():
        return timestamp.strftime("%Y-%m-%d")
    return timestamp.isoformat()


def _coerce_panel(panel: PanelInput, *, name: str = "panel") -> pd.DataFrame:
    if isinstance(panel, pd.DataFrame):
        frame = panel.copy(deep=True)
    elif isinstance(panel, Mapping):
        dates = panel.get("dates")
        codes = panel.get("codes")
        values = panel.get("values")
        if not isinstance(dates, list) or not isinstance(codes, list):
            raise ValueError(f"{name} 面板载荷缺少 dates/codes")
        frame = pd.DataFrame(
            values,
            index=pd.to_datetime(dates),
            columns=[str(code) for code in codes],
            dtype=float,
        )
    else:
        raise TypeError(f"{name} 必须是 DataFrame 或面板载荷")

    if isinstance(frame.columns, pd.MultiIndex):
        raise ValueError(f"{name} 必须是 date × code 单字段面板")
    if frame.index.has_duplicates:
        raise ValueError(f"{name} 日期索引不能重复")
    if frame.columns.has_duplicates:
        raise ValueError(f"{name} 股票代码不能重复")
    frame.index = pd.DatetimeIndex(pd.to_datetime(frame.index))
    frame.columns = [str(column) for column in frame.columns]
    frame = frame.sort_index(kind="stable")
    frame = frame.reindex(sorted(frame.columns), axis=1)
    return frame.apply(pd.to_numeric, errors="coerce").replace(
        [np.inf, -np.inf], np.nan
    )


def _panel_payload(frame: pd.DataFrame) -> PanelPayload:
    clean = _coerce_panel(frame)
    return {
        "dates": [_date_text(date) for date in clean.index],
        "codes": list(clean.columns),
        "values": [
            [_safe_number(value) for value in row]
            for row in clean.to_numpy(dtype=float)
        ],
    }


def _series_payload(series: pd.Series) -> dict[str, Any]:
    clean = series.copy(deep=True)
    clean.index = pd.DatetimeIndex(pd.to_datetime(clean.index))
    clean = clean.sort_index(kind="stable")
    return {
        "dates": [_date_text(date) for date in clean.index],
        "values": [_safe_number(value) for value in clean.to_numpy()],
    }


def _correlation(
    left: pd.Series,
    right: pd.Series,
    *,
    rank: bool,
    min_samples: int,
) -> tuple[float | None, int]:
    aligned = pd.concat([left, right], axis=1).replace(
        [np.inf, -np.inf], np.nan
    ).dropna()
    count = len(aligned)
    if count < min_samples:
        return None, count
    x = aligned.iloc[:, 0]
    y = aligned.iloc[:, 1]
    if rank:
        x = x.rank(method="average")
        y = y.rank(method="average")
    if float(x.std(ddof=0)) <= _EPSILON or float(y.std(ddof=0)) <= _EPSILON:
        return None, count
    value = float(x.corr(y))
    return (_safe_number(value), count)


def _summary(values: Sequence[float | None]) -> dict[str, Any]:
    finite = np.asarray(
        [value for value in values if value is not None and math.isfinite(value)],
        dtype=float,
    )
    count = int(finite.size)
    if count == 0:
        return {
            "count": 0,
            "mean": None,
            "std": None,
            "icir": None,
            "positive_ratio": None,
            "t_stat": None,
        }
    mean = float(finite.mean())
    std = float(finite.std(ddof=1)) if count >= 2 else None
    ratio = (
        mean / std
        if std is not None and std > _EPSILON
        else None
    )
    t_stat = (
        mean / (std / math.sqrt(count))
        if std is not None and std > _EPSILON
        else None
    )
    return {
        "count": count,
        "mean": _safe_number(mean),
        "std": _safe_number(std),
        "icir": _safe_number(ratio),
        "positive_ratio": _safe_number(float((finite > 0).mean())),
        "t_stat": _safe_number(t_stat),
    }


def cross_sectional_preprocess(
    factor: PanelInput,
    *,
    missing: Literal["median", "drop"] = "median",
    winsor_method: Literal["mad", "quantile", "none"] = "mad",
    mad_scale: float = 3.0,
    lower_quantile: float = 0.01,
    upper_quantile: float = 0.99,
    zscore: bool = True,
    min_samples: int = 2,
) -> dict[str, Any]:
    """Impute, winsorize, and optionally z-score each date independently."""
    if missing not in {"median", "drop"}:
        raise ValueError("missing 必须是 median 或 drop")
    if winsor_method not in {"mad", "quantile", "none"}:
        raise ValueError("winsor_method 必须是 mad、quantile 或 none")
    if not math.isfinite(float(mad_scale)) or mad_scale <= 0:
        raise ValueError("mad_scale 必须大于 0")
    if not 0 <= lower_quantile < upper_quantile <= 1:
        raise ValueError("分位数边界必须满足 0 <= lower < upper <= 1")
    if min_samples < 1:
        raise ValueError("min_samples 必须大于 0")

    source = _coerce_panel(factor, name="factor")
    output = pd.DataFrame(np.nan, index=source.index, columns=source.columns)
    diagnostics: list[dict[str, Any]] = []

    for date, row in source.iterrows():
        values = row.copy()
        finite = values.dropna()
        diagnostic: dict[str, Any] = {
            "date": _date_text(date),
            "n_total": int(len(values)),
            "n_valid_input": int(len(finite)),
            "n_missing_input": int(values.isna().sum()),
            "imputed": 0,
            "winsor_lower": None,
            "winsor_upper": None,
            "pre_zscore_mean": None,
            "pre_zscore_std": None,
            "status": "ok",
        }
        if len(finite) < min_samples:
            diagnostic["status"] = "insufficient_samples"
            diagnostics.append(diagnostic)
            continue

        if missing == "median":
            fill_value = float(finite.median())
            diagnostic["imputed"] = int(values.isna().sum())
            values = values.fillna(fill_value)

        current = values.dropna()
        lower: float | None = None
        upper: float | None = None
        if winsor_method == "mad":
            median = float(current.median())
            mad = float((current - median).abs().median())
            if mad > _EPSILON:
                robust_sigma = 1.4826 * mad
                lower = median - mad_scale * robust_sigma
                upper = median + mad_scale * robust_sigma
        elif winsor_method == "quantile":
            lower = float(current.quantile(lower_quantile))
            upper = float(current.quantile(upper_quantile))
        if lower is not None and upper is not None:
            values = values.clip(lower=lower, upper=upper)
            diagnostic["winsor_lower"] = _safe_number(lower)
            diagnostic["winsor_upper"] = _safe_number(upper)

        current = values.dropna()
        mean = float(current.mean())
        std = float(current.std(ddof=0))
        diagnostic["pre_zscore_mean"] = _safe_number(mean)
        diagnostic["pre_zscore_std"] = _safe_number(std)
        if zscore:
            if std <= _EPSILON:
                values.loc[current.index] = 0.0
                diagnostic["status"] = "constant_cross_section"
            else:
                values.loc[current.index] = (current - mean) / std
        output.loc[date] = values
        diagnostics.append(diagnostic)

    return {
        "values": _panel_payload(output),
        "diagnostics": diagnostics,
        "config": {
            "missing": missing,
            "winsor_method": winsor_method,
            "mad_scale": float(mad_scale),
            "lower_quantile": float(lower_quantile),
            "upper_quantile": float(upper_quantile),
            "zscore": bool(zscore),
            "min_samples": int(min_samples),
        },
    }


def _categorical_row(
    industries: pd.DataFrame | pd.Series | Mapping[str, Any],
    date: pd.Timestamp,
    codes: pd.Index,
) -> pd.Series:
    if isinstance(industries, pd.DataFrame):
        frame = industries.copy(deep=True)
        frame.index = pd.DatetimeIndex(pd.to_datetime(frame.index))
        if frame.index.has_duplicates:
            raise ValueError("industries 日期索引不能重复")
        frame.columns = frame.columns.map(str)
        if frame.columns.has_duplicates:
            raise ValueError("industries 股票代码不能重复")
        if date not in frame.index:
            return pd.Series(index=codes, dtype=object)
        row = frame.loc[date]
    elif isinstance(industries, pd.Series):
        row = industries.copy(deep=True)
    else:
        row = pd.Series(dict(industries), dtype=object)
    row.index = row.index.map(str)
    if row.index.has_duplicates:
        raise ValueError("industries 股票代码不能重复")
    return row.reindex(codes).astype("object")


def _numeric_row(
    values: PanelInput | pd.Series | Mapping[str, Any],
    date: pd.Timestamp,
    codes: pd.Index,
) -> pd.Series:
    if isinstance(values, pd.DataFrame) or (
        isinstance(values, Mapping) and {"dates", "codes", "values"} <= set(values)
    ):
        frame = _coerce_panel(values, name="market_caps")
        if date not in frame.index:
            return pd.Series(np.nan, index=codes, dtype=float)
        return frame.loc[date].reindex(codes)
    if isinstance(values, pd.Series):
        row = values.copy(deep=True)
    else:
        row = pd.Series(dict(values), dtype=float)
    row.index = row.index.map(str)
    if row.index.has_duplicates:
        raise ValueError("market_caps 股票代码不能重复")
    return pd.to_numeric(row.reindex(codes), errors="coerce").replace(
        [np.inf, -np.inf], np.nan
    )


def neutralize_industry_size(
    factor: PanelInput,
    industries: pd.DataFrame | pd.Series | Mapping[str, Any],
    market_caps: PanelInput | pd.Series | Mapping[str, Any],
    *,
    min_samples: int = 3,
) -> dict[str, Any]:
    """Neutralize each date against industry dummies and log market cap."""
    if min_samples < 2:
        raise ValueError("min_samples 必须至少为 2")
    source = _coerce_panel(factor, name="factor")
    residuals = pd.DataFrame(np.nan, index=source.index, columns=source.columns)
    exposures: list[dict[str, Any]] = []

    for date, factor_row in source.iterrows():
        industry_row = _categorical_row(industries, date, source.columns)
        cap_row = _numeric_row(market_caps, date, source.columns)
        valid = (
            factor_row.notna()
            & industry_row.notna()
            & cap_row.notna()
            & (cap_row > 0)
        )
        codes = source.columns[valid]
        y = factor_row.loc[codes].astype(float)
        industry = industry_row.loc[codes].map(str)
        log_cap = np.log(cap_row.loc[codes].astype(float))
        categories = sorted(industry.unique())
        baseline = categories[0] if categories else None
        diagnostic: dict[str, Any] = {
            "date": _date_text(date),
            "n_obs": int(len(codes)),
            "n_dropped": int(len(source.columns) - len(codes)),
            "baseline_industry": baseline,
            "rank": None,
            "n_features": None,
            "r_squared": None,
            "status": "ok",
            "coefficients": {},
        }
        if len(codes) < min_samples:
            if len(codes):
                residuals.loc[date, codes] = y - float(y.mean())
                diagnostic["coefficients"] = {
                    "intercept": _safe_number(float(y.mean())),
                    "log_market_cap": None,
                }
            diagnostic["status"] = "demean_fallback"
            exposures.append(diagnostic)
            continue

        design: dict[str, np.ndarray] = {
            "intercept": np.ones(len(codes), dtype=float)
        }
        for category in categories[1:]:
            design[f"industry::{category}"] = (
                industry == category
            ).to_numpy(dtype=float)
        if float(log_cap.std(ddof=0)) > _EPSILON:
            design["log_market_cap"] = log_cap.to_numpy(dtype=float)

        feature_names = list(design)
        x = np.column_stack([design[name] for name in feature_names])
        diagnostic["n_features"] = len(feature_names)
        if len(codes) <= len(feature_names):
            residuals.loc[date, codes] = y - float(y.mean())
            diagnostic["coefficients"] = {
                "intercept": _safe_number(float(y.mean())),
                "log_market_cap": None,
            }
            diagnostic["status"] = "demean_fallback"
            exposures.append(diagnostic)
            continue

        coefficients, _, rank, _ = np.linalg.lstsq(
            x, y.to_numpy(dtype=float), rcond=None
        )
        fitted = x @ coefficients
        residual = y.to_numpy(dtype=float) - fitted
        residuals.loc[date, codes] = residual
        tss = float(np.square(y.to_numpy(dtype=float) - float(y.mean())).sum())
        rss = float(np.square(residual).sum())
        coefficient_map = {
            name: _safe_number(value)
            for name, value in zip(feature_names, coefficients)
        }
        if baseline is not None:
            coefficient_map[f"industry::{baseline}"] = 0.0
        coefficient_map.setdefault("log_market_cap", None)
        diagnostic["coefficients"] = dict(sorted(coefficient_map.items()))
        diagnostic["rank"] = int(rank)
        diagnostic["r_squared"] = (
            _safe_number(1.0 - rss / tss) if tss > _EPSILON else None
        )
        if rank < x.shape[1]:
            diagnostic["status"] = "rank_deficient"
        exposures.append(diagnostic)

    return {
        "residuals": _panel_payload(residuals),
        "exposures": exposures,
        "config": {"min_samples": int(min_samples)},
    }


def _exposure_fit(
    y: pd.Series,
    industry: pd.Series | None,
    log_size: pd.Series | None,
) -> tuple[np.ndarray, list[str], np.ndarray, int]:
    design: dict[str, np.ndarray] = {
        "intercept": np.ones(len(y), dtype=float),
    }
    if industry is not None:
        categories = sorted(industry.astype(str).unique())
        for category in categories[1:]:
            design[f"industry::{category}"] = (
                industry.astype(str) == category
            ).to_numpy(dtype=float)
    if log_size is not None and float(log_size.std(ddof=0)) > _EPSILON:
        design["log_market_cap"] = log_size.to_numpy(dtype=float)
    names = list(design)
    matrix = np.column_stack([design[name] for name in names])
    coefficients, _, rank, _ = np.linalg.lstsq(
        matrix,
        y.to_numpy(dtype=float),
        rcond=None,
    )
    return coefficients, names, matrix, int(rank)


def _exposure_snapshot(
    y: pd.Series,
    coefficients: np.ndarray,
    names: list[str],
    matrix: np.ndarray,
    *,
    baseline_industry: str | None = None,
) -> dict[str, Any]:
    fitted = matrix @ coefficients
    values = y.to_numpy(dtype=float)
    residual = values - fitted
    tss = float(np.square(values - float(values.mean())).sum())
    rss = float(np.square(residual).sum())
    coefficient_map = {
        name: _safe_number(value)
        for name, value in zip(names, coefficients)
    }
    return {
        "r_squared": (
            _safe_number(1.0 - rss / tss) if tss > _EPSILON else None
        ),
        "intercept": coefficient_map.get("intercept"),
        "baseline_industry": baseline_industry,
        "industry_coefficients": {
            **({baseline_industry: 0.0} if baseline_industry else {}),
            **{
                name.removeprefix("industry::"): value
                for name, value in coefficient_map.items()
                if name.startswith("industry::")
            },
        },
        "log_market_cap": coefficient_map.get("log_market_cap"),
    }


def neutralize_factor_exposures(
    factor: PanelInput,
    *,
    mode: Literal["industry", "size", "industry+size"],
    industries: pd.DataFrame | None = None,
    market_caps: PanelInput | None = None,
    min_samples: int = 10,
) -> dict[str, Any]:
    """Strictly neutralize each trading-date cross-section.

    The design matrix is rebuilt independently for every date.  Missing
    exposures are never forward-filled and small/rank-deficient sections are
    excluded rather than silently advertised as neutralized.
    """

    if mode not in {"industry", "size", "industry+size"}:
        raise ValueError("mode 必须为 industry、size 或 industry+size")
    if min_samples < 3:
        raise ValueError("min_samples 必须至少为 3")
    needs_industry = mode in {"industry", "industry+size"}
    needs_size = mode in {"size", "industry+size"}
    if needs_industry and industries is None:
        raise ValueError("行业中性化缺少逐日行业面板")
    if needs_size and market_caps is None:
        raise ValueError("规模中性化缺少逐日市值面板")

    source = _coerce_panel(factor, name="factor")
    industry_frame: pd.DataFrame | None = None
    if industries is not None:
        industry_frame = industries.copy(deep=True)
        industry_frame.index = pd.DatetimeIndex(
            pd.to_datetime(industry_frame.index)
        )
        industry_frame.columns = industry_frame.columns.map(str)
        if (
            industry_frame.index.has_duplicates
            or industry_frame.columns.has_duplicates
        ):
            raise ValueError("行业面板日期或股票代码不能重复")
        industry_frame = industry_frame.reindex(
            index=source.index,
            columns=source.columns,
        )
    size_frame: pd.DataFrame | None = None
    if market_caps is not None:
        size_frame = _coerce_panel(market_caps, name="market_caps").reindex(
            index=source.index,
            columns=source.columns,
        )

    residuals = pd.DataFrame(
        np.nan,
        index=source.index,
        columns=source.columns,
    )
    diagnostics: list[dict[str, Any]] = []
    aggregate_dropped = {
        "factor_missing": 0,
        "industry_missing": 0,
        "size_missing_or_nonpositive": 0,
    }
    successful_observations = 0

    for date, factor_row in source.iterrows():
        factor_valid = factor_row.notna() & np.isfinite(
            factor_row.to_numpy(dtype=float, na_value=np.nan)
        )
        valid = factor_valid.copy()
        industry_row: pd.Series | None = None
        if needs_industry and industry_frame is not None:
            industry_row = industry_frame.loc[date]
            industry_valid = industry_row.notna() & industry_row.map(
                lambda item: bool(str(item).strip())
            )
            valid &= industry_valid
        else:
            industry_valid = pd.Series(True, index=source.columns)
        cap_row: pd.Series | None = None
        if needs_size and size_frame is not None:
            cap_row = pd.to_numeric(
                size_frame.loc[date],
                errors="coerce",
            ).replace([np.inf, -np.inf], np.nan)
            size_valid = cap_row.notna() & (cap_row > 0)
            valid &= size_valid
        else:
            size_valid = pd.Series(True, index=source.columns)

        dropped = {
            "factor_missing": int((~factor_valid).sum()),
            "industry_missing": int((factor_valid & ~industry_valid).sum())
            if needs_industry
            else 0,
            "size_missing_or_nonpositive": int(
                (factor_valid & industry_valid & ~size_valid).sum()
            )
            if needs_size
            else 0,
        }
        for reason, count in dropped.items():
            aggregate_dropped[reason] += count
        codes = source.columns[valid]
        y = factor_row.loc[codes].astype(float)
        selected_industry = (
            industry_row.loc[codes].astype(str)
            if needs_industry and industry_row is not None
            else None
        )
        baseline_industry = (
            sorted(selected_industry.unique())[0]
            if selected_industry is not None and len(selected_industry)
            else None
        )
        selected_log_size = (
            np.log(cap_row.loc[codes].astype(float))
            if needs_size and cap_row is not None
            else None
        )
        diagnostic: dict[str, Any] = {
            "date": _date_text(date),
            "status": "ok",
            "sample_count": int(len(codes)),
            "candidate_count": int(len(source.columns)),
            "coverage_ratio": _safe_number(
                len(codes) / len(source.columns)
            )
            if len(source.columns)
            else None,
            "dropped_by_reason": dropped,
            "rank": None,
            "feature_count": None,
            "before": None,
            "after": None,
        }
        if len(codes) < min_samples:
            diagnostic["status"] = "insufficient_samples"
            diagnostics.append(diagnostic)
            continue

        coefficients, names, matrix, rank = _exposure_fit(
            y,
            selected_industry,
            selected_log_size,
        )
        diagnostic["rank"] = rank
        diagnostic["feature_count"] = len(names)
        if len(codes) <= len(names) or rank < len(names):
            diagnostic["status"] = "rank_deficient"
            diagnostics.append(diagnostic)
            continue

        residual = y.to_numpy(dtype=float) - matrix @ coefficients
        residual_series = pd.Series(residual, index=codes)
        after_coefficients, after_names, after_matrix, after_rank = (
            _exposure_fit(
                residual_series,
                selected_industry,
                selected_log_size,
            )
        )
        if after_rank < len(after_names):
            diagnostic["status"] = "rank_deficient"
            diagnostics.append(diagnostic)
            continue
        residuals.loc[date, codes] = residual
        diagnostic["before"] = _exposure_snapshot(
            y,
            coefficients,
            names,
            matrix,
            baseline_industry=baseline_industry,
        )
        diagnostic["after"] = _exposure_snapshot(
            residual_series,
            after_coefficients,
            after_names,
            after_matrix,
            baseline_industry=baseline_industry,
        )
        diagnostics.append(diagnostic)
        successful_observations += len(codes)

    successful = [
        item for item in diagnostics if item["status"] == "ok"
    ]

    def mean_metric(section: str, metric: str) -> float | None:
        values = [
            item[section][metric]
            for item in successful
            if isinstance(item.get(section), dict)
            and item[section].get(metric) is not None
        ]
        return _safe_number(float(np.mean(values))) if values else None

    possible = int(source.shape[0] * source.shape[1])
    return {
        "schema_version": "factor-neutralization/v1",
        "mode": mode,
        "method": "daily_cross_sectional_ols",
        "fit_window": "same_trading_date_only",
        "residuals": _panel_payload(residuals),
        "daily": diagnostics,
        "summary": {
            "dates_total": len(diagnostics),
            "dates_neutralized": len(successful),
            "dates_excluded": len(diagnostics) - len(successful),
            "observations_neutralized": int(successful_observations),
            "possible_observations": possible,
            "coverage_ratio": _safe_number(
                successful_observations / possible
            )
            if possible
            else None,
            "dropped_by_reason": aggregate_dropped,
            "mean_r_squared_before": mean_metric("before", "r_squared"),
            "mean_r_squared_after": mean_metric("after", "r_squared"),
        },
        "config": {
            "min_samples": int(min_samples),
            "uses_industry": needs_industry,
            "uses_size": needs_size,
        },
    }


def _price_frame(prices: pd.DataFrame, field: str) -> pd.DataFrame:
    source = prices.copy(deep=True)
    source.index = pd.DatetimeIndex(pd.to_datetime(source.index))
    if source.index.has_duplicates:
        raise ValueError("prices 日期索引不能重复")
    source = source.sort_index(kind="stable")
    if not isinstance(source.columns, pd.MultiIndex):
        return _coerce_panel(source, name="prices")
    values: dict[str, pd.Series] = {}
    aliases = (field, field.lower(), field.upper(), field.capitalize())
    codes = sorted({str(column[0]) for column in source.columns})
    for code in codes:
        for alias in aliases:
            if (code, alias) in source.columns:
                values[code] = pd.to_numeric(
                    source[(code, alias)], errors="coerce"
                )
                break
    if not values:
        raise ValueError(f"prices 缺少 {field} 字段")
    return pd.DataFrame(values, index=source.index).replace(
        [np.inf, -np.inf], np.nan
    )


def compute_forward_returns(
    prices: pd.DataFrame,
    *,
    horizons: Sequence[int] = (1, 5, 20),
    evaluation_end: str | pd.Timestamp | None = None,
    price_field: str = "close",
) -> dict[str, Any]:
    """Create session-based forward returns without crossing evaluation_end."""
    raw_horizons = list(horizons)
    if any(
        isinstance(value, (bool, np.bool_))
        or not isinstance(value, (int, np.integer))
        for value in raw_horizons
    ):
        raise ValueError("horizons 必须包含正整数")
    normalized_horizons = sorted(set(int(value) for value in raw_horizons))
    if not normalized_horizons or any(value <= 0 for value in normalized_horizons):
        raise ValueError("horizons 必须包含正整数")
    close = _price_frame(prices, price_field)
    if close.empty:
        raise ValueError("prices 不能为空")
    boundary = (
        pd.Timestamp(evaluation_end)
        if evaluation_end is not None
        else close.index.max()
    )
    if pd.isna(boundary):
        raise ValueError("evaluation_end 不是有效日期")
    eligible = close.loc[close.index <= boundary].copy()
    results: dict[str, PanelPayload] = {}
    for horizon in normalized_horizons:
        forward = eligible.shift(-horizon).div(eligible).sub(1)
        results[str(horizon)] = _panel_payload(forward)
    return {
        "horizons": results,
        "evaluation_end": _date_text(boundary),
        "price_field": price_field,
    }


def calculate_ic(
    factor: PanelInput,
    forward_returns: PanelInput,
    *,
    min_samples: int = 5,
) -> dict[str, Any]:
    """Calculate daily Pearson IC and Spearman RankIC plus summary statistics."""
    if min_samples < 2:
        raise ValueError("min_samples 必须至少为 2")
    factor_frame = _coerce_panel(factor, name="factor")
    return_frame = _coerce_panel(forward_returns, name="forward_returns")
    dates = factor_frame.index.intersection(return_frame.index).sort_values()
    codes = sorted(set(factor_frame.columns).intersection(return_frame.columns))
    series: list[dict[str, Any]] = []
    pearson_values: list[float | None] = []
    rank_values: list[float | None] = []
    for date in dates:
        pearson, count = _correlation(
            factor_frame.loc[date, codes],
            return_frame.loc[date, codes],
            rank=False,
            min_samples=min_samples,
        )
        rank_ic, _ = _correlation(
            factor_frame.loc[date, codes],
            return_frame.loc[date, codes],
            rank=True,
            min_samples=min_samples,
        )
        pearson_values.append(pearson)
        rank_values.append(rank_ic)
        series.append(
            {
                "date": _date_text(date),
                "sample_count": int(count),
                "pearson_ic": pearson,
                "rank_ic": rank_ic,
            }
        )
    return {
        "series": series,
        "summary": {
            "pearson_ic": _summary(pearson_values),
            "rank_ic": _summary(rank_values),
        },
        "min_samples": int(min_samples),
    }


def analyze_factor_decay(
    factor: PanelInput,
    forward_returns_by_horizon: Mapping[int | str, PanelInput],
    *,
    min_samples: int = 5,
) -> dict[str, Any]:
    """Summarize IC and RankIC across deterministic ascending horizons."""
    normalized: dict[int, PanelInput] = {}
    for raw_horizon, returns in forward_returns_by_horizon.items():
        if isinstance(raw_horizon, bool):
            raise ValueError("衰减 horizon 必须为正整数")
        try:
            horizon = int(raw_horizon)
        except (TypeError, ValueError):
            raise ValueError("衰减 horizon 必须为正整数") from None
        if str(horizon) != str(raw_horizon) and not isinstance(
            raw_horizon, (int, np.integer)
        ):
            raise ValueError("衰减 horizon 必须为正整数")
        if horizon <= 0 or horizon in normalized:
            raise ValueError("衰减 horizon 必须为不重复的正整数")
        normalized[horizon] = returns
    points: list[dict[str, Any]] = []
    for horizon in sorted(normalized):
        result = calculate_ic(
            factor,
            normalized[horizon],
            min_samples=min_samples,
        )
        points.append(
            {
                "horizon": horizon,
                "pearson_ic": result["summary"]["pearson_ic"],
                "rank_ic": result["summary"]["rank_ic"],
            }
        )
    return {"points": points, "min_samples": int(min_samples)}


def analyze_quantile_returns(
    factor: PanelInput,
    forward_returns: PanelInput,
    *,
    quantiles: int = 5,
    min_samples: int = 10,
) -> dict[str, Any]:
    """Compute daily quantile returns, high-minus-low spread, and monotonicity."""
    if quantiles < 2:
        raise ValueError("quantiles 必须至少为 2")
    if min_samples < quantiles:
        raise ValueError("min_samples 不能小于 quantiles")
    factor_frame = _coerce_panel(factor, name="factor")
    return_frame = _coerce_panel(forward_returns, name="forward_returns")
    dates = factor_frame.index.intersection(return_frame.index).sort_values()
    codes = sorted(set(factor_frame.columns).intersection(return_frame.columns))
    daily: list[dict[str, Any]] = []
    group_values: dict[int, list[float]] = {
        group: [] for group in range(1, quantiles + 1)
    }
    spreads: list[float] = []

    for date in dates:
        aligned = pd.concat(
            [
                factor_frame.loc[date, codes].rename("factor"),
                return_frame.loc[date, codes].rename("forward_return"),
            ],
            axis=1,
        ).dropna().sort_index()
        if len(aligned) < min_samples:
            continue
        deterministic_rank = aligned["factor"].rank(
            method="first", ascending=True
        )
        labels = pd.qcut(
            deterministic_rank,
            q=quantiles,
            labels=list(range(1, quantiles + 1)),
        ).astype(int)
        means = aligned.groupby(labels, observed=True)["forward_return"].mean()
        group_returns = {
            str(group): _safe_number(means.get(group))
            for group in range(1, quantiles + 1)
        }
        spread = float(means.loc[quantiles] - means.loc[1])
        spreads.append(spread)
        for group in range(1, quantiles + 1):
            group_values[group].append(float(means.loc[group]))
        daily.append(
            {
                "date": _date_text(date),
                "sample_count": int(len(aligned)),
                "group_returns": group_returns,
                "long_short_spread": _safe_number(spread),
            }
        )

    mean_returns = {
        str(group): (
            _safe_number(float(np.mean(values))) if values else None
        )
        for group, values in group_values.items()
    }
    finite_group_means = [
        mean_returns[str(group)] for group in range(1, quantiles + 1)
    ]
    monotonicity: float | None = None
    if all(value is not None for value in finite_group_means):
        monotonicity = _correlation(
            pd.Series(range(1, quantiles + 1), dtype=float),
            pd.Series(finite_group_means, dtype=float),
            rank=True,
            min_samples=2,
        )[0]
    return {
        "series": daily,
        "mean_group_returns": mean_returns,
        "long_short": _summary(spreads),
        "monotonicity": monotonicity,
        "quantiles": int(quantiles),
        "min_samples": int(min_samples),
    }


def factor_correlation_matrix(
    factors: Mapping[str, PanelInput],
    *,
    method: Literal["pearson", "spearman"] = "spearman",
    min_samples: int = 5,
) -> dict[str, Any]:
    """Average independent daily cross-sectional correlations."""
    if method not in {"pearson", "spearman"}:
        raise ValueError("method 必须是 pearson 或 spearman")
    if min_samples < 2:
        raise ValueError("min_samples 必须至少为 2")
    names = sorted(str(name) for name in factors)
    if len(names) != len(set(names)):
        raise ValueError("因子名称转为字符串后不能重复")
    frames = {
        str(name): _coerce_panel(panel, name=str(name))
        for name, panel in factors.items()
    }
    size = len(names)
    matrix: list[list[float | None]] = [
        [None for _ in range(size)] for _ in range(size)
    ]
    date_counts: list[list[int]] = [
        [0 for _ in range(size)] for _ in range(size)
    ]
    for left_index, left_name in enumerate(names):
        left = frames[left_name]
        valid_self_dates = 0
        for date in left.index:
            self_correlation, _ = _correlation(
                left.loc[date],
                left.loc[date],
                rank=method == "spearman",
                min_samples=min_samples,
            )
            if self_correlation is not None:
                valid_self_dates += 1
        matrix[left_index][left_index] = (
            1.0 if valid_self_dates else None
        )
        date_counts[left_index][left_index] = valid_self_dates
        for right_index in range(left_index + 1, size):
            right_name = names[right_index]
            left = frames[left_name]
            right = frames[right_name]
            dates = left.index.intersection(right.index).sort_values()
            codes = sorted(set(left.columns).intersection(right.columns))
            values: list[float] = []
            for date in dates:
                correlation, _ = _correlation(
                    left.loc[date, codes],
                    right.loc[date, codes],
                    rank=method == "spearman",
                    min_samples=min_samples,
                )
                if correlation is not None:
                    values.append(correlation)
            value = _safe_number(float(np.mean(values))) if values else None
            matrix[left_index][right_index] = value
            matrix[right_index][left_index] = value
            date_counts[left_index][right_index] = len(values)
            date_counts[right_index][left_index] = len(values)
    return {
        "factors": names,
        "matrix": matrix,
        "valid_date_counts": date_counts,
        "method": method,
        "min_samples": int(min_samples),
    }


def attribute_portfolio_returns(
    portfolio_returns: pd.Series,
    industry_factor_returns: pd.DataFrame,
    size_factor_returns: pd.Series | None = None,
    *,
    min_samples: int = 20,
) -> dict[str, Any]:
    """Regress portfolio returns on industry and size factor returns."""
    if min_samples < 3:
        raise ValueError("min_samples 必须至少为 3")
    portfolio = pd.to_numeric(
        portfolio_returns.copy(deep=True), errors="coerce"
    ).rename("portfolio")
    portfolio.index = pd.DatetimeIndex(pd.to_datetime(portfolio.index))
    if portfolio.index.has_duplicates:
        raise ValueError("portfolio_returns 日期索引不能重复")
    industry = industry_factor_returns.copy(deep=True)
    industry.index = pd.DatetimeIndex(pd.to_datetime(industry.index))
    if industry.index.has_duplicates:
        raise ValueError("industry_factor_returns 日期索引不能重复")
    industry.columns = [f"industry::{column}" for column in industry.columns]
    if industry.columns.has_duplicates:
        raise ValueError("行业因子名称不能重复")
    industry = industry.reindex(sorted(industry.columns), axis=1).apply(
        pd.to_numeric, errors="coerce"
    )
    parts: list[pd.Series | pd.DataFrame] = [portfolio, industry]
    if size_factor_returns is not None:
        size = pd.to_numeric(
            size_factor_returns.copy(deep=True), errors="coerce"
        ).rename("log_size")
        size.index = pd.DatetimeIndex(pd.to_datetime(size.index))
        if size.index.has_duplicates:
            raise ValueError("size_factor_returns 日期索引不能重复")
        parts.append(size)
    aligned = pd.concat(parts, axis=1).replace(
        [np.inf, -np.inf], np.nan
    ).dropna().sort_index(kind="stable")
    feature_names = [
        column for column in aligned.columns if column != "portfolio"
    ]
    empty_result = {
        "status": "insufficient_samples",
        "diagnostics": {
            "sample_count": int(len(aligned)),
            "rank": None,
            "n_features": len(feature_names) + 1,
            "r_squared": None,
        },
        "exposures": {
            name: None for name in ["intercept", *feature_names]
        },
        "average_daily_contribution": {
            name: None for name in ["intercept", *feature_names, "residual"]
        },
        "cumulative_linear_contribution": {
            name: None for name in ["intercept", *feature_names, "residual"]
        },
        "residual_returns": {"dates": [], "values": []},
    }
    if len(aligned) < max(min_samples, len(feature_names) + 2):
        return empty_result

    y = aligned["portfolio"].to_numpy(dtype=float)
    x = np.column_stack(
        [
            np.ones(len(aligned), dtype=float),
            aligned[feature_names].to_numpy(dtype=float),
        ]
    )
    names = ["intercept", *feature_names]
    coefficients, _, rank, _ = np.linalg.lstsq(x, y, rcond=None)
    fitted = x @ coefficients
    residual = y - fitted
    tss = float(np.square(y - float(y.mean())).sum())
    rss = float(np.square(residual).sum())
    exposures = {
        name: _safe_number(value)
        for name, value in zip(names, coefficients)
    }
    average_contribution: dict[str, Any] = {
        "intercept": _safe_number(coefficients[0])
    }
    cumulative_contribution: dict[str, Any] = {
        "intercept": _safe_number(coefficients[0] * len(aligned))
    }
    for position, name in enumerate(feature_names, start=1):
        average_contribution[name] = _safe_number(
            coefficients[position] * float(aligned[name].mean())
        )
        cumulative_contribution[name] = _safe_number(
            coefficients[position] * float(aligned[name].sum())
        )
    average_contribution["residual"] = _safe_number(float(residual.mean()))
    cumulative_contribution["residual"] = _safe_number(float(residual.sum()))
    return {
        "status": "ok" if rank == x.shape[1] else "rank_deficient",
        "diagnostics": {
            "sample_count": int(len(aligned)),
            "rank": int(rank),
            "n_features": int(x.shape[1]),
            "r_squared": (
                _safe_number(1.0 - rss / tss) if tss > _EPSILON else None
            ),
        },
        "exposures": exposures,
        "average_daily_contribution": average_contribution,
        "cumulative_linear_contribution": cumulative_contribution,
        "residual_returns": _series_payload(
            pd.Series(residual, index=aligned.index)
        ),
    }


# Readable aliases for callers migrating from notebook terminology.
preprocess_cross_sectional = cross_sectional_preprocess
neutralize_factor = neutralize_industry_size
compute_ic = calculate_ic
factor_decay = analyze_factor_decay
quantile_group_returns = analyze_quantile_returns
portfolio_exposure_attribution = attribute_portfolio_returns
