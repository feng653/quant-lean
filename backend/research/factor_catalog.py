"""Extensible factor catalog shared by research and exported strategies."""

from __future__ import annotations

from collections.abc import Callable
import hashlib
import json
import re
from typing import Any

import numpy as np
import pandas as pd

FactorBuilder = Callable[[pd.DataFrame], pd.DataFrame]
_BUILDERS: dict[tuple[str, str], FactorBuilder] = {}
FACTOR_CATALOG: list[dict[str, object]] = []
_VERSION = re.compile(r"[1-9]\d*\.\d+\.\d+")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def factor_definition_digest(definition: dict[str, object]) -> str:
    """Return the immutable digest of a trusted, code-registered definition."""
    unsigned = {
        key: value
        for key, value in definition.items()
        if key not in {"definition_digest", "status", "revision"}
    }
    return hashlib.sha256(_canonical_json(unsigned).encode("utf-8")).hexdigest()


def register_factor(
    factor_id: str,
    *,
    name: str,
    description: str,
    lookback: int,
    required_fields: tuple[str, ...] = ("close",),
    category: str = "price",
    parameters: dict[str, object] | None = None,
    version: str = "1.0.0",
    parameter_schema: dict[str, object] | None = None,
    dependencies: tuple[tuple[str, str], ...] = (),
    supersedes: str | None = None,
) -> Callable[[FactorBuilder], FactorBuilder]:
    """Register one reviewed implementation and its non-executable manifest.

    This decorator is the only factor registration boundary.  HTTP payloads can
    change lifecycle state for an exact digest, but can never provide a builder
    or an expression.
    """
    if not required_fields or any(not item.strip() for item in required_fields):
        raise ValueError("required_fields must contain non-empty field names")
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", factor_id):
        raise ValueError("factor_id must be a stable snake_case identifier")
    if not _VERSION.fullmatch(version):
        raise ValueError("factor version must use MAJOR.MINOR.PATCH")
    if supersedes is not None and not _VERSION.fullmatch(supersedes):
        raise ValueError("supersedes must use MAJOR.MINOR.PATCH")
    normalized_dependencies = [
        {"factor_id": dependency_id, "version": dependency_version}
        for dependency_id, dependency_version in dependencies
    ]
    for dependency in normalized_dependencies:
        if (
            not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", dependency["factor_id"])
            or not _VERSION.fullmatch(dependency["version"])
        ):
            raise ValueError("factor dependencies must contain valid IDs and versions")
    normalized_parameter_schema = parameter_schema or {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            key: {"const": value}
            for key, value in sorted((parameters or {}).items())
        },
    }
    if normalized_parameter_schema.get("type") != "object":
        raise ValueError("parameter_schema root type must be object")
    if normalized_parameter_schema.get("additionalProperties") is not False:
        raise ValueError("parameter_schema must fail closed on unknown parameters")

    def decorator(builder: FactorBuilder) -> FactorBuilder:
        identity = (factor_id, version)
        if identity in _BUILDERS:
            raise ValueError(f"factor version already registered: {factor_id}@{version}")
        definition: dict[str, object] = {
            "factor_id": factor_id,
            "version": version,
            "name": name,
            "description": description,
            "direction": "high",
            "lookback": lookback,
            "required_fields": list(dict.fromkeys(required_fields)),
            "category": category,
            "parameters": dict(parameters or {}),
            "parameter_schema": normalized_parameter_schema,
            "dependencies": normalized_dependencies,
            "supersedes": supersedes,
        }
        definition["definition_digest"] = factor_definition_digest(definition)
        _BUILDERS[identity] = builder
        FACTOR_CATALOG.append(definition)
        return builder
    return decorator


def _field(pivot: pd.DataFrame, name: str) -> pd.DataFrame:
    if not isinstance(pivot.columns, pd.MultiIndex):
        raise ValueError("行情缓存必须使用 (code, field) 列契约")
    values = {
        str(code): pd.to_numeric(pivot[(code, name)], errors="coerce")
        for code in sorted({str(column[0]) for column in pivot.columns})
        if (code, name) in pivot.columns
    }
    if not values:
        raise ValueError(f"行情缓存缺少 {name} 字段")
    return pd.DataFrame(values, index=pivot.index)


@register_factor(
    "momentum_20",
    name="20 日动量",
    description="过去 20 个交易日收盘价收益率。",
    lookback=20,
    category="momentum",
    parameters={"window": 20},
)
def _momentum_20(pivot: pd.DataFrame) -> pd.DataFrame:
    return _field(pivot, "close").pct_change(20, fill_method=None)


@register_factor(
    "short_reversal_5",
    name="5 日反转",
    description="过去 5 个交易日收益率的相反数。",
    lookback=5,
    category="reversal",
    parameters={"window": 5},
)
def _short_reversal_5(pivot: pd.DataFrame) -> pd.DataFrame:
    return -_field(pivot, "close").pct_change(5, fill_method=None)


@register_factor(
    "low_volatility_20",
    name="20 日低波动",
    description="20 日收益波动率的相反数。",
    lookback=20,
    category="risk",
    parameters={"window": 20},
)
def _low_volatility_20(pivot: pd.DataFrame) -> pd.DataFrame:
    returns = _field(pivot, "close").pct_change(fill_method=None)
    return -returns.rolling(20, min_periods=20).std()


@register_factor(
    "liquidity_20",
    name="20 日流动性",
    description="20 日平均成交额的对数。",
    lookback=20,
    required_fields=("amount",),
    category="liquidity",
    parameters={"window": 20},
)
def _liquidity_20(pivot: pd.DataFrame) -> pd.DataFrame:
    amount = _field(pivot, "amount")
    return np.log1p(amount.clip(lower=0)).rolling(20, min_periods=20).mean()


@register_factor(
    "price_efficiency_20",
    name="20 日价格效率",
    description="20 日净价格变化相对逐日绝对变化的比例，衡量趋势路径效率。",
    lookback=20,
    category="quality",
    parameters={"window": 20},
)
def _price_efficiency_20(pivot: pd.DataFrame) -> pd.DataFrame:
    close = _field(pivot, "close")
    path = close.diff().abs().rolling(20, min_periods=20).sum()
    displacement = close.diff(20)
    return displacement.divide(path.where(path > 0))


@register_factor(
    "risk_adjusted_momentum_20",
    name="20 日风险调整动量",
    description="20 日收益除以同期日收益波动率，兼顾趋势方向与路径风险。",
    lookback=20,
    category="momentum",
    parameters={"window": 20},
)
def _risk_adjusted_momentum_20(pivot: pd.DataFrame) -> pd.DataFrame:
    close = _field(pivot, "close")
    daily = close.pct_change(fill_method=None)
    volatility = daily.rolling(20, min_periods=20).std()
    return close.pct_change(20, fill_method=None).divide(
        volatility.where(volatility > 0)
    )


def get_factor_definition(
    factor_id: str,
    version: str | None = None,
) -> dict[str, object]:
    candidates = [
        item
        for item in FACTOR_CATALOG
        if item["factor_id"] == factor_id
        and (version is None or item["version"] == version)
    ]
    if not candidates:
        raise ValueError("未知因子版本")
    # Code registration order is deterministic.  Explicit versions are used
    # for old evidence; the last registered version is the current default.
    return dict(candidates[-1])


def build_factor_panel(
    pivot: pd.DataFrame,
    factor_id: str,
    version: str | None = None,
) -> pd.DataFrame:
    definition = get_factor_definition(factor_id, version)
    try:
        builder = _BUILDERS[(factor_id, str(definition["version"]))]
    except KeyError as exc:
        raise ValueError("未知因子") from exc
    return builder(pivot)
