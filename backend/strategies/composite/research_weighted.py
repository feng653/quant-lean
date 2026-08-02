"""Manifest-bound, static-weight composite for reviewed research candidates."""

from __future__ import annotations

import json
import math
import re
from typing import Any

import pandas as pd

from backend.core.types import SignalDict
from backend.strategies.base import (
    ParamField,
    PortfolioSignalMode,
    RetrainFrequency,
    StrategyCategory,
    StrategyMetadata,
    StrategyMode,
    SubStrategyRef,
)
from backend.strategies.composite._common import (
    DEFAULT_SUB_STRATEGIES,
    RuleCompositeStrategy,
)
from backend.strategies.registry import get_registry


_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_DEFAULT_SPECS = [
    {"params": {}, "strategy_id": strategy_id}
    for strategy_id in DEFAULT_SUB_STRATEGIES[:3]
]
_DEFAULT_WEIGHTS = [1 / 3, 1 / 3, 1 / 3]


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class CompositeResearchWeightedStrategy(RuleCompositeStrategy):
    """Run reviewed atomic strategy parameters with immutable static weights.

    ``component_specs`` deliberately accepts data, not code. Candidate source
    experiment and manifest identities are retained in the parent experiment's
    canonical parameter hash, but the runtime never loads or executes an
    artifact from those experiments.
    """

    @classmethod
    def metadata(cls) -> StrategyMetadata:
        return StrategyMetadata(
            strategy_id="composite_research_weighted_v1",
            display_name="研究证据静态权重组合",
            version="1.0.0",
            category=StrategyCategory.COMPOSITE,
            description=(
                "按经审查的单策略实验参数与静态权重聚合信号；来源实验和 PIT "
                "清单哈希随组合参数固化。"
            ),
            supported_modes=[StrategyMode.BATCH],
            requires_training=False,
            retrain_frequency=RetrainFrequency.NEVER,
            estimated_training_seconds=0,
            portfolio_signal_mode=PortfolioSignalMode.EVENT_ORDERS,
            params=[
                ParamField(
                    "component_specs",
                    "str",
                    _canonical_json(_DEFAULT_SPECS),
                    "JSON 子策略定义（策略 ID、参数及可选来源清单）",
                ),
                ParamField(
                    "static_weights",
                    "str",
                    _canonical_json(_DEFAULT_WEIGHTS),
                    "与子策略顺序一致的 JSON 非负权重数组",
                ),
            ],
            sub_strategies=[
                SubStrategyRef(strategy_id=item, role="可替换的原子信号源")
                for item in DEFAULT_SUB_STRATEGIES[:3]
            ],
            integration_method="manifest_bound_static_weight",
            tags=["组合策略", "静态权重", "研究证据", "PIT"],
        )

    @staticmethod
    def _decode_specs(params: dict[str, Any]) -> list[dict[str, Any]]:
        raw = params.get("component_specs", _canonical_json(_DEFAULT_SPECS))
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > 32_768:
            raise ValueError("component_specs 必须是 32KiB 内的 JSON 字符串")
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("component_specs 不是合法 JSON") from exc
        if not isinstance(decoded, list) or not 2 <= len(decoded) <= 8:
            raise ValueError("component_specs 必须包含 2..8 个子策略")
        return decoded

    @staticmethod
    def _decode_weights(params: dict[str, Any], size: int) -> list[float]:
        raw = params.get("static_weights", _canonical_json(_DEFAULT_WEIGHTS))
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > 4_096:
            raise ValueError("static_weights 必须是 4KiB 内的 JSON 字符串")
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("static_weights 不是合法 JSON") from exc
        if not isinstance(decoded, list) or len(decoded) != size:
            raise ValueError("static_weights 数量必须与 component_specs 一致")
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) < 0
            for value in decoded
        ):
            raise ValueError("static_weights 必须全部是有限非负数")
        total = math.fsum(float(value) for value in decoded)
        if total <= 0:
            raise ValueError("static_weights 至少一个权重大于 0")
        return [float(value) / total for value in decoded]

    def _validated_components(
        self,
        params: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], list[float]]:
        specs = self._decode_specs(params)
        weights = self._decode_weights(params, len(specs))
        registry = get_registry()
        strategy_ids: list[str] = []
        normalized: list[dict[str, Any]] = []
        for item in specs:
            if not isinstance(item, dict) or set(item) - {
                "strategy_id",
                "params",
                "source_experiment_id",
                "source_manifest_hash",
            }:
                raise ValueError("component_specs 含未知字段或非对象项")
            strategy_id = item.get("strategy_id")
            child_params = item.get("params", {})
            if not isinstance(strategy_id, str) or not strategy_id:
                raise ValueError("component_specs.strategy_id 无效")
            if strategy_id in strategy_ids:
                raise ValueError("component_specs 不能重复引用策略")
            if not isinstance(child_params, dict):
                raise ValueError("component_specs.params 必须是对象")
            try:
                metadata = registry.get_metadata(strategy_id)
            except KeyError as exc:
                raise ValueError(f"未知子策略: {strategy_id}") from exc
            if metadata.category in {
                StrategyCategory.COMPOSITE,
                StrategyCategory.PORTFOLIO,
                StrategyCategory.ML,
            } or metadata.requires_training:
                raise ValueError(f"组合只接受非机器学习原子策略: {strategy_id}")
            valid, error = registry.validate_params(strategy_id, child_params)
            if not valid:
                raise ValueError(f"{strategy_id} 参数无效: {error}")
            source_id = item.get("source_experiment_id")
            source_hash = item.get("source_manifest_hash")
            if (source_id is None) != (source_hash is None):
                raise ValueError("来源实验 ID 与清单哈希必须同时提供")
            if source_id is not None and (
                isinstance(source_id, bool)
                or not isinstance(source_id, int)
                or source_id <= 0
                or not isinstance(source_hash, str)
                or _HEX64.fullmatch(source_hash) is None
            ):
                raise ValueError("来源实验或清单哈希无效")
            strategy_ids.append(strategy_id)
            normalized.append(
                {
                    "strategy_id": strategy_id,
                    "params": child_params,
                    **(
                        {
                            "source_experiment_id": source_id,
                            "source_manifest_hash": source_hash,
                        }
                        if source_id is not None
                        else {}
                    ),
                }
            )
        return normalized, weights

    def validate_params(self, params: dict) -> tuple[bool, str]:
        try:
            self._validated_components(params)
        except ValueError as exc:
            return False, str(exc)
        return True, ""

    def generate_batch_signals(
        self,
        pivot: pd.DataFrame,
        params: dict,
        start_date: str,
        end_date: str,
    ) -> SignalDict:
        specs, weights = self._validated_components(params)
        signals = [
            self._get_sub_strategy(item["strategy_id"]).generate_batch_signals(
                pivot,
                item["params"],
                start_date,
                end_date,
            )
            for item in specs
        ]
        return self._merge_signals(signals, weights)
