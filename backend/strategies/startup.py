"""策略注册启动扫描（原 main._scan_strategies）。"""

from __future__ import annotations

import logging

from backend.config import settings
from backend.data.factor_governance import FactorGovernanceStore
from backend.strategies.factor._configured_factor import (
    load_factor_strategy_definitions,
    make_factor_strategy_class,
)
from backend.strategies.registry import get_registry

logger = logging.getLogger("quant_platform")


async def scan_strategies() -> None:
    """启动时扫描策略目录，注册所有策略。"""
    try:
        registry = get_registry()
        strategies_dir = settings.PROJECT_ROOT / "backend" / "strategies"
        registry.scan_directory(strategies_dir)
        for definition in load_factor_strategy_definitions():
            try:
                registry.register_strategy_class(
                    make_factor_strategy_class(definition)
                )
            except ValueError:
                logger.exception(
                    "Failed to register exported factor strategy %s",
                    definition.get("strategy_id"),
                )
        for definition in (
            FactorGovernanceStore().list_active_strategy_definitions()
        ):
            try:
                registry.replace_strategy_class(
                    make_factor_strategy_class(definition)
                )
            except ValueError:
                logger.exception(
                    "Failed to register governed factor strategy %s",
                    definition.get("strategy_id"),
                )
        logger.info(
            "Strategies scanned: %d loaded", len(registry.list_all())
        )
    except Exception:
        logger.exception("Strategy scan failed — continuing without strategies")
