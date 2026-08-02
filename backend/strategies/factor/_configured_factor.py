"""Data-defined factor combinations; definitions are data, never executable code."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import threading
from pathlib import Path
from typing import Any

import pandas as pd

from backend.config import settings
from backend.core.types import SignalDict
from backend.research.factor_catalog import (
    FACTOR_CATALOG,
    build_factor_panel,
    get_factor_definition,
)
from backend.strategies.base import (
    ParamField,
    PortfolioSignalMode,
    RetrainFrequency,
    StrategyCategory,
    StrategyMetadata,
    StrategyMode,
    StrategyProtocol,
)
from backend.strategies.factor._common import (
    cross_sectional_rank,
    ranked_monthly_signals,
)

DEFINITION_SCHEMA = "factor-combination-strategy/v1"
_DEFINITION_LOCK = threading.Lock()


def _definitions_path() -> Path:
    path = settings.abs_path(settings.DATABASE_DIR) / "factor_strategies.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


class ConfiguredFactorStrategy(StrategyProtocol):
    point_in_time_context_capability = "dated_cross_section"
    definition: dict[str, Any] = {}

    @classmethod
    def metadata(cls) -> StrategyMetadata:
        definition = cls.definition
        description = " + ".join(
            (
                f'{get_factor_definition(item["factor_id"], item.get("factor_version"))["name"]}'
                f'×{item["weight"]:g}'
            )
            for item in definition["components"]
        )
        legacy_unbound = (
            definition.get("schema_version") == DEFINITION_SCHEMA
            or bool(definition.get("legacy_unbound"))
            or not bool(definition.get("research_evidence"))
        )
        return StrategyMetadata(
            strategy_id=definition["strategy_id"],
            display_name=definition["name"],
            version=definition["version"],
            category=StrategyCategory.FACTOR,
            description=(
                f"由因子研究导出的只读组合：{description}"
                + ("（旧版未绑定研究证据，不可晋级）" if legacy_unbound else "")
            ),
            supported_modes=[StrategyMode.BATCH],
            requires_training=False,
            retrain_frequency=RetrainFrequency.NEVER,
            estimated_training_seconds=0,
            portfolio_signal_mode=PortfolioSignalMode.TARGET_WEIGHTS,
            params=[
                ParamField(
                    "top_k_pct",
                    "float",
                    definition["top_k_pct"],
                    "买入综合得分最高的股票比例",
                    min=0.01,
                    max=1,
                )
            ],
            tags=[
                "因子研究导出",
                "多因子",
                "配置固化",
                "无动态代码",
                *(
                    ["legacy_unbound", "不可晋级"]
                    if legacy_unbound
                    else ["证据绑定", "版本治理"]
                ),
            ],
        )

    def validate_params(self, params: dict) -> tuple[bool, str]:
        value = params.get("top_k_pct", self.definition["top_k_pct"])
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False, "top_k_pct 必须为数字"
        return (0 < float(value) <= 1, "top_k_pct 必须在 (0, 1] 范围内")

    def generate_batch_signals(
        self,
        pivot: pd.DataFrame,
        params: dict,
        start_date: str,
        end_date: str,
    ) -> SignalDict:
        combined: pd.DataFrame | None = None
        total_weight = 0.0
        for component in self.definition["components"]:
            weight = float(component["weight"])
            ranked = cross_sectional_rank(
                build_factor_panel(
                    pivot,
                    component["factor_id"],
                    component.get("factor_version"),
                )
            )
            combined = (
                ranked * weight
                if combined is None
                else combined.add(ranked * weight)
            )
            total_weight += weight
        if combined is None or total_weight <= 0:
            return {}
        return ranked_monthly_signals(
            combined / total_weight,
            params,
            start_date,
            end_date,
        )


def make_factor_strategy_class(
    definition: dict[str, Any],
) -> type[ConfiguredFactorStrategy]:
    class_name = "ExportedFactor_" + re.sub(
        r"[^0-9A-Za-z_]", "_", definition["strategy_id"]
    )
    return type(
        class_name,
        (ConfiguredFactorStrategy,),
        {"definition": definition, "__module__": __name__},
    )


def load_factor_strategy_definitions() -> list[dict[str, Any]]:
    path = _definitions_path()
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("factor strategy definitions must be a list")
    result = []
    known_factors = {
        str(item["factor_id"]) for item in FACTOR_CATALOG
    }
    for item in payload:
        if not isinstance(item, dict) or item.get("schema_version") != DEFINITION_SCHEMA:
            raise ValueError("invalid factor strategy definition schema")
        strategy_id = str(item.get("strategy_id") or "")
        components = item.get("components")
        if (
            not re.fullmatch(r"factor_combo_[0-9a-f]{12}", strategy_id)
            or not isinstance(item.get("name"), str)
            or not str(item["name"]).strip()
            or not isinstance(components, list)
            or not components
        ):
            raise ValueError("invalid factor strategy identity")
        component_ids = [str(component.get("factor_id")) for component in components]
        weights = [component.get("weight") for component in components]
        if (
            len(set(component_ids)) != len(component_ids)
            or any(factor_id not in known_factors for factor_id in component_ids)
            or any(
                isinstance(weight, bool)
                or not isinstance(weight, (int, float))
                or not math.isfinite(float(weight))
                or float(weight) <= 0
                for weight in weights
            )
        ):
            raise ValueError("invalid factor strategy components")
        top_k_pct = item.get("top_k_pct")
        research_evidence = item.get("research_evidence", [])
        if (
            isinstance(top_k_pct, bool)
            or not isinstance(top_k_pct, (int, float))
            or not math.isfinite(float(top_k_pct))
            or not 0 < float(top_k_pct) <= 1
            or len(str(item["name"])) > 80
            or item.get("version") != "1.0.0"
            or not isinstance(item.get("owner_user_id"), int)
            or not isinstance(research_evidence, list)
        ):
            raise ValueError("invalid factor strategy metadata")
        for evidence in research_evidence:
            if (
                not isinstance(evidence, dict)
                or not re.fullmatch(r"frun_[0-9a-f]{32}", str(evidence.get("run_id", "")))
                or not re.fullmatch(r"[0-9a-f]{64}", str(evidence.get("dataset_digest", "")))
                or not re.fullmatch(r"[0-9a-f]{64}", str(evidence.get("result_digest", "")))
                or str(evidence.get("factor_id", "")) not in known_factors
            ):
                raise ValueError("invalid factor strategy research evidence")
        canonical = json.dumps(
            {key: value for key, value in item.items() if key != "definition_sha256"},
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        combo_digest = hashlib.sha256(
            json.dumps(
                {"components": components, "top_k_pct": top_k_pct},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if item.get("definition_sha256") != digest or strategy_id != (
            "factor_combo_" + combo_digest[:12]
        ):
            raise ValueError("factor strategy definition integrity mismatch")
        result.append(item)
    return result


def export_factor_strategy(
    *,
    name: str,
    components: list[dict[str, Any]],
    top_k_pct: float,
    owner_user_id: int,
    research_evidence: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    normalized_name = name.strip()
    known_factors = {
        str(item["factor_id"]) for item in FACTOR_CATALOG
    }
    if not normalized_name or len(normalized_name) > 80:
        raise ValueError("策略名称必须为 1..80 个非空字符")
    if (
        isinstance(owner_user_id, bool)
        or not isinstance(owner_user_id, int)
        or owner_user_id <= 0
    ):
        raise ValueError("owner_user_id 无效")
    if (
        isinstance(top_k_pct, bool)
        or not isinstance(top_k_pct, (int, float))
        or not math.isfinite(float(top_k_pct))
        or not 0 < float(top_k_pct) <= 1
    ):
        raise ValueError("top_k_pct 必须为 (0, 1] 内的有限数字")
    if not isinstance(components, list) or not 1 <= len(components) <= 20:
        raise ValueError("components 必须包含 1..20 个因子")
    normalized_components: list[dict[str, Any]] = []
    seen_factors: set[str] = set()
    for component in components:
        factor_id = str(component.get("factor_id") or "")
        weight = component.get("weight")
        if (
            factor_id not in known_factors
            or factor_id in seen_factors
            or isinstance(weight, bool)
            or not isinstance(weight, (int, float))
            or not math.isfinite(float(weight))
            or not 0 < float(weight) <= 100
        ):
            raise ValueError("components 包含未知、重复或无效权重的因子")
        seen_factors.add(factor_id)
        normalized_components.append(
            {"factor_id": factor_id, "weight": float(weight)}
        )
    normalized_evidence: list[dict[str, str]] = []
    seen_runs: set[str] = set()
    for evidence in research_evidence or []:
        run_id = str(evidence.get("run_id") or "")
        factor_id = str(evidence.get("factor_id") or "")
        dataset_digest = str(evidence.get("dataset_digest") or "")
        result_digest = str(evidence.get("result_digest") or "")
        if (
            not re.fullmatch(r"frun_[0-9a-f]{32}", run_id)
            or run_id in seen_runs
            or factor_id not in seen_factors
            or not re.fullmatch(r"[0-9a-f]{64}", dataset_digest)
            or not re.fullmatch(r"[0-9a-f]{64}", result_digest)
        ):
            raise ValueError("research_evidence 包含无效、重复或未选因子的研究证据")
        seen_runs.add(run_id)
        normalized_evidence.append(
            {
                "run_id": run_id,
                "factor_id": factor_id,
                "dataset_digest": dataset_digest,
                "result_digest": result_digest,
            }
        )

    canonical = json.dumps(
        {
            "components": normalized_components,
            "top_k_pct": float(top_k_pct),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    strategy_id = "factor_combo_" + hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()[:12]
    definition: dict[str, Any] = {
        "schema_version": DEFINITION_SCHEMA,
        "strategy_id": strategy_id,
        "name": normalized_name,
        "version": "1.0.0",
        "components": normalized_components,
        "top_k_pct": float(top_k_pct),
        "owner_user_id": owner_user_id,
        "research_evidence": normalized_evidence,
        # File-backed v1 definitions remain runnable for compatibility but are
        # never part of the transactional SQLite version/promotion chain.
        "legacy_unbound": True,
    }
    definition["definition_sha256"] = hashlib.sha256(
        json.dumps(
            definition,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    path = _definitions_path()
    with _DEFINITION_LOCK:
        current = load_factor_strategy_definitions()
        if any(item["strategy_id"] == strategy_id for item in current):
            raise ValueError("相同因子组合已存在于策略池")
        previous = path.read_bytes() if path.exists() else None
        current.append(definition)
        temp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        try:
            temp.write_text(
                json.dumps(current, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temp, path)
            from backend.strategies.registry import get_registry
            get_registry().register_strategy_class(
                make_factor_strategy_class(definition)
            )
        except Exception:
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                rollback = path.with_name(
                    f".{path.name}.{secrets.token_hex(8)}.rollback"
                )
                rollback.write_bytes(previous)
                os.replace(rollback, path)
            raise
        finally:
            temp.unlink(missing_ok=True)
    return definition
